"""CME FedWatch 방식 확률 계산 엔진.

30-Day Fed Funds 선물(ZQ) 가격 → 회의별 정책금리 시나리오 확률.

계산 흐름
---------
1. implied_rate(월) = 100 - 선물가격          … 해당 월 EFFR 평균의 시장 내재값
2. spread = 실제 EFFR - 현 목표범위 중값       … EFFR이 중값과 미세하게 어긋나는 것을 보정
3. 회의별로 회의 직후 금리 r_post 를 순차 부트스트랩
   - Method B (우선): 다음 달에 회의가 없으면 다음 달 계약을 그대로 읽는다  → 레버리지 100%
   - Method A       : 회의 월 계약을 일수 가중 분해
                      avg = (d/D)·r_pre + ((D-d)/D)·r_post
                      (회의 종료일 d 다음날부터 새 금리 적용)
4. 누적 인상 횟수 기대값 N_i = (r_post_i - 현 중값) / 0.25
5. 이항 트리로 회의별 시나리오 분포 생성 (증분 Δ = N_i - N_{i-1})

주의: CME QuikStrike 는 실시간 호가 기반, 본 엔진은 일일 정산가 기반이므로
      수 %p 차이가 날 수 있다. 특히 월말에 붙은 회의는 민감도가 커진다.
"""
from __future__ import annotations

import calendar
import datetime as dt
import math
from dataclasses import dataclass, field
from typing import Dict, List, Tuple

STEP = 0.25  # 1회 조정폭 (%)
MIN_LEVERAGE = 0.20  # Method A 신뢰 하한: 새 금리가 적용되는 월내 일수 비중


@dataclass
class MeetingOutlook:
    date: dt.date
    method: str                      # "next-month" | "day-weighted"
    confidence: str                  # "OK" | "LOW"
    r_post: float                    # 회의 직후 시장 내재 정책금리(중값 기준, %)
    cum_moves: float                 # 현 수준 대비 누적 25bp 조정 횟수 기대값
    delta_moves: float               # 직전 회의 대비 증분
    scenarios: List[Tuple[int, float]] = field(default_factory=list)
    # (조정횟수 k, 확률) — k>0 인상, k<0 인하, k=0 동결


def month_key(d: dt.date) -> Tuple[int, int]:
    return (d.year, d.month)


def add_month(key: Tuple[int, int], n: int = 1) -> Tuple[int, int]:
    y, m = key
    idx = (y * 12 + (m - 1)) + n
    return (idx // 12, idx % 12 + 1)


def _split(expected: float) -> Dict[int, float]:
    """기대 조정횟수 expected 를 인접 정수 두 개에 배분."""
    lo = math.floor(expected)
    frac = expected - lo
    if frac <= 1e-9:
        return {lo: 1.0}
    return {lo: 1.0 - frac, lo + 1: frac}


def build_tree(deltas: List[float]) -> List[Dict[int, float]]:
    """회의별 증분 기대치 리스트 → 회의별 누적 조정횟수 분포."""
    dist: Dict[int, float] = {0: 1.0}
    out: List[Dict[int, float]] = []
    for delta in deltas:
        split = _split(delta)
        nxt: Dict[int, float] = {}
        for k, p in dist.items():
            for step, w in split.items():
                if w <= 0:
                    continue
                nxt[k + step] = nxt.get(k + step, 0.0) + p * w
        # 수치 잡음 제거
        dist = {k: v for k, v in nxt.items() if v > 1e-6}
        total = sum(dist.values())
        dist = {k: v / total for k, v in dist.items()}
        out.append(dict(sorted(dist.items())))
    return out


def compute(
    meetings: List[dt.date],
    implied: Dict[Tuple[int, int], float],
    effr: float,
    target_lo: float,
    target_hi: float,
    all_meetings: List[dt.date],
) -> Tuple[List[MeetingOutlook], Dict[str, float]]:
    """회의별 전망과 진단값을 계산한다.

    meetings   : 전망 대상 회의(가까운 순)
    implied    : {(연,월): 100 - 선물가} 내재금리
    effr       : 실효 연방기금금리 (%)
    target_lo/hi: 현 목표범위 (%)
    all_meetings: 전체 회의 일정(다음 달 회의 유무 판정용)
    """
    mid = (target_lo + target_hi) / 2.0
    spread = effr - mid  # 통상 +0.5 ~ +1.5bp

    def tgt(key: Tuple[int, int]) -> float | None:
        v = implied.get(key)
        return None if v is None else v - spread

    # 진단: 회의가 없는 최근 월의 내재금리가 현 중값과 일치하는지
    anchor_gap = None
    today = dt.date.today()
    for off in (0, 1):
        k = add_month(month_key(today), off)
        if not any(m.year == k[0] and m.month == k[1] for m in all_meetings):
            v = tgt(k)
            if v is not None:
                anchor_gap = round((v - mid) * 100, 2)  # bp
                break

    outlooks: List[MeetingOutlook] = []
    r_pre = mid
    cum_prev = 0.0
    deltas: List[float] = []

    for mdate in meetings:
        key = month_key(mdate)
        nkey = add_month(key, 1)
        next_has_meeting = any(m.year == nkey[0] and m.month == nkey[1] for m in all_meetings)

        r_post = None
        method = ""
        confidence = "OK"

        if not next_has_meeting and tgt(nkey) is not None:
            r_post = tgt(nkey)
            method = "next-month"
        else:
            avg = tgt(key)
            D = calendar.monthrange(key[0], key[1])[1]
            d = mdate.day
            leverage = (D - d) / D if avg is not None else 0.0
            if leverage > 0 and avg is not None:
                r_post = (avg * D - r_pre * d) / (D - d)
                method = "day-weighted"
                if leverage < MIN_LEVERAGE:
                    confidence = "LOW"
            elif tgt(nkey) is not None:
                # 회의가 월 마지막 날 — 다음 달 계약으로 대체(다음 회의분이 일부 섞임)
                r_post = tgt(nkey)
                method = "next-month(contaminated)"
                confidence = "LOW"
            else:
                break  # 데이터 누락 — 이후 회의는 부트스트랩 체인이 성립하지 않는다

        cum = (r_post - mid) / STEP
        deltas.append(cum - cum_prev)
        outlooks.append(MeetingOutlook(
            date=mdate, method=method, confidence=confidence,
            r_post=r_post, cum_moves=cum, delta_moves=cum - cum_prev,
        ))
        r_pre = r_post
        cum_prev = cum

    for outlook, dist in zip(outlooks, build_tree(deltas)):
        outlook.scenarios = sorted(dist.items(), key=lambda kv: kv[0])

    diag = {
        "spread_bp": round(spread * 100, 2),
        "anchor_gap_bp": anchor_gap,
        "target_mid": mid,
    }
    return outlooks, diag


def range_label(k: int, target_lo: float, target_hi: float) -> str:
    return f"{target_lo + STEP * k:.2f}~{target_hi + STEP * k:.2f}%"


def move_label(k: int) -> str:
    if k == 0:
        return "동결"
    bp = abs(k) * 25
    return f"{'인상' if k > 0 else '인하'} {bp}bp"
