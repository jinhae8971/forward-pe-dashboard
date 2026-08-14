# FedWatch Radar

30-Day Fed Funds 선물(ZQ) 기반 FOMC 정책금리 시나리오 확률을 매일 산출해
텔레그램으로 발송하는 서버리스 자동화.

## 왜 CME를 직접 긁지 않는가

CME 웹사이트(`CmeWS` 엔드포인트 포함)는 Data Terms of Use 상 자동 수집이 금지되어
있고 실제로 IP 차단이 걸린다. 공식 FedWatch REST API는 OAuth 기반 유료·엔타이틀먼트
상품이다. 따라서 **동일한 방법론을 자체 구현**하고 원천 데이터만 공개 경로에서 받는다.

| 항목 | 출처 | 인증 |
|---|---|---|
| ZQ 월물 정산가 | Yahoo Finance (`ZQ{월코드}{연2}.CBT`, query1/query2 이중화) | 불필요 |
| EFFR · 현 목표범위 | 뉴욕 연준 `markets.newyorkfed.org` 공개 API | 불필요 |
| FOMC 일정 | `federalreserve.gov` 파싱 (실패 시 내장 표 폴백) | 불필요 |

## 계산 방법

1. `내재금리(월) = 100 − 선물가` — 해당 월 EFFR 평균의 시장 내재값
2. `spread = 실제 EFFR − 목표범위 중값` 을 빼서 목표금리 공간으로 환산
3. 회의별 직후 금리 `r_post` 순차 부트스트랩
   - **Method B (우선)**: 다음 달에 회의가 없으면 다음 달 계약을 그대로 읽음 → 레버리지 100%
   - **Method A**: 회의 월 계약 일수 가중 분해
     `avg = (d/D)·r_pre + ((D−d)/D)·r_post` (회의 종료일 d 다음날부터 새 금리 적용)
   - 새 금리 적용 일수 비중이 20% 미만이면 `LOW` 신뢰도로 표시(⚠️)
4. 누적 조정 횟수 `N = (r_post − 현 중값) / 0.25`
5. 증분 `Δ = N_i − N_{i−1}` 로 이항 트리를 전개해 회의별 시나리오 분포 산출

### 자체 검증(앵커 체크)

회의가 없는 달의 내재금리는 현 목표범위 중값과 일치해야 한다.
브리프 하단에 `앵커오차 ±x bp` 로 노출되며, 통상 1bp 이내면 정상이다.

> 일일 정산가 기반이므로 CME QuikStrike 실시간값과 수 %p 차이가 날 수 있다.
> 특히 월말에 붙은 회의는 계산 민감도가 커진다(⚠️ 표기).

## 구성

```
fedwatch_radar.py     수집 · 메시지 · 발송 · 이력
fedwatch_engine.py    확률 계산 엔진 (순수 함수, 네트워크 없음)
fomc_calendar.py      FOMC 일정 파싱 + 폴백 표
tests/                유닛 테스트 24건 (워크플로우 게이트)
data/latest.json      최신 스냅샷
data/history.json     일자별 이력 (1D·1W 증감 산출용, 400일 보관)
```

## 스케줄

`cron: "30 22 * * 1-5"` (UTC) = **KST 화~토 07:30**
미국 월~금 정산가를 다음 날 아침에 받는다.

## 배포

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass

.\setup_github.ps1              # 신규 레포(fedwatch-radar)로 단독 운영
.\push_to_existing_repo.ps1     # TELEGRAM_TOKEN 보유 레포의 fedwatch/ 에 얹기
```

필요 Secret: `TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID`
등록 위치: `https://github.com/<user>/<repo>/settings/secrets/actions`

## 운영 메모

- FOMC 일정은 매 실행 시 연준 페이지에서 다시 읽는다. 파싱 실패 시 `fomc_calendar.py`
  의 `FALLBACK_MEETINGS` 로 폴백하며, 브리프 하단에 `일정: fallback(날짜)` 로 표시된다.
  이 표기가 며칠 이상 지속되면 파싱 로직을 점검한다.
- 같은 시세일에 재실행하면 `data/` 변경이 없어 커밋이 생기지 않는다(멱등).
- `workflow_dispatch` 의 `skip_notify=true` 로 발송 없이 계산만 검증할 수 있다.
