#!/usr/bin/env python3
"""CME FedWatch 레이더 — 금리선물 내재 확률 일간 브리프.

데이터 경로
-----------
- 30-Day Fed Funds 선물(ZQ) 월물 정산가 : Yahoo Finance (query1/query2 이중화)
- 실효 연방기금금리·현 목표범위        : 뉴욕 연준 공개 API (인증 불필요)
- FOMC 일정                            : federalreserve.gov (실패 시 내장 표)

CME 웹사이트는 Data Terms of Use 상 자동 수집이 금지되어 있어 직접 호출하지 않는다.
동일한 FedWatch 방법론(30-Day Fed Funds 선물 기반)을 자체 구현해 산출한다.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
import time
from typing import Dict, List, Tuple

import requests

import fedwatch_engine as eng
from fomc_calendar import load_meetings, upcoming

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(ROOT, "data")
LATEST_PATH = os.path.join(DATA_DIR, "latest.json")
HISTORY_PATH = os.path.join(DATA_DIR, "history.json")

MONTH_CODE = {1: "F", 2: "G", 3: "H", 4: "J", 5: "K", 6: "M",
              7: "N", 8: "Q", 9: "U", 10: "V", 11: "X", 12: "Z"}
HORIZON_MONTHS = 9       # 조회할 월물 수 (표시 회의 + 다음달 계약까지면 충분)
MEETINGS_SHOWN = 4       # 브리프에 표시할 회의 수
HISTORY_KEEP = 400       # 이력 보관 일수

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"


# ────────────────────────────── 설정 ──────────────────────────────

def load_config() -> dict:
    cfg = {
        "telegram_token": os.environ.get("TELEGRAM_TOKEN", ""),
        "telegram_chat_id": os.environ.get("TELEGRAM_CHAT_ID", ""),
    }
    path = os.path.join(ROOT, "config.json")
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            print(f"[config] 읽기 실패 - 환경변수만 사용: {exc}")
            data = {}
        for k, v in data.items():
            key = k.lower()
            if key in cfg and not cfg[key]:
                cfg[key] = v
    return cfg


# ────────────────────────────── 수집 ──────────────────────────────

def _get_json(url: str, params: dict | None = None, tries: int = 3) -> dict:
    last = None
    for i in range(tries):
        try:
            r = requests.get(url, params=params, headers={"User-Agent": UA,
                             "Accept": "application/json"}, timeout=25)
            r.raise_for_status()
            return r.json()
        except Exception as exc:  # noqa: BLE001
            last = exc
            # 429(레이트리밋)는 공유 러너 IP에서 흔하다 — 더 길게 물러선다
            wait = 8.0 * (i + 1) if "429" in str(exc) else 1.5 * (i + 1)
            time.sleep(wait)
    raise RuntimeError(f"요청 실패 {url}: {last}")


def fetch_policy_rate() -> dict:
    """뉴욕 연준 공개 API에서 EFFR과 현 목표범위를 가져온다."""
    j = _get_json("https://markets.newyorkfed.org/api/rates/unsecured/effr/last/1.json")
    row = j["refRates"][0]
    return {
        "date": row["effectiveDate"],
        "effr": float(row["percentRate"]),
        "target_lo": float(row["targetRateFrom"]),
        "target_hi": float(row["targetRateTo"]),
    }


def _sym(key: Tuple[int, int]) -> str:
    return f"ZQ{MONTH_CODE[key[1]]}{key[0] % 100:02d}.CBT"


def _fetch_spark(symbols: List[str]) -> Dict[str, Tuple[float, int]]:
    """Yahoo spark — 여러 심볼을 1회 요청으로. 공유 러너 IP의 429를 피하는 핵심."""
    out: Dict[str, Tuple[float, int]] = {}
    for host in ("query1", "query2"):
        try:
            j = _get_json(f"https://{host}.finance.yahoo.com/v7/finance/spark",
                          params={"symbols": ",".join(symbols), "range": "5d",
                                  "interval": "1d"}, tries=3)
            for entry in (j.get("spark") or {}).get("result", []):
                resp = (entry.get("response") or [{}])[0]
                meta = resp.get("meta", {})
                price, ts = meta.get("regularMarketPrice"), meta.get("regularMarketTime")
                if price:
                    out[entry["symbol"]] = (float(price), int(ts or 0))
            if out:
                return out
        except Exception as exc:  # noqa: BLE001
            print(f"[yahoo/spark] @{host} 실패: {exc}")
    return out


def _fetch_chart(symbol: str) -> Tuple[float, int] | None:
    """개별 계약 조회 — spark 누락분 보완용 폴백."""
    for host in ("query1", "query2"):
        try:
            j = _get_json(f"https://{host}.finance.yahoo.com/v8/finance/chart/{symbol}",
                          params={"range": "5d", "interval": "1d"}, tries=2)
            res = (j.get("chart") or {}).get("result")
            if res:
                meta = res[0].get("meta", {})
                price, ts = meta.get("regularMarketPrice"), meta.get("regularMarketTime")
                if price:
                    return float(price), int(ts or 0)
        except Exception as exc:  # noqa: BLE001
            print(f"[yahoo/chart] {symbol} @{host} 실패: {exc}")
    return None


def fetch_zq_curve(start: dt.date, months: int = HORIZON_MONTHS
                   ) -> Tuple[Dict[Tuple[int, int], float], str]:
    """ZQ 월물 곡선 수집 → {(연,월): 내재금리(100-가격)} 및 시세일자.

    1순위 spark(1회 요청) → 누락분만 개별 chart 로 보완.
    일부 원월이 비어도 계산 가능한 근월까지는 진행한다.
    """
    keys = []
    key = (start.year, start.month)
    for _ in range(months):
        keys.append(key)
        key = eng.add_month(key, 1)

    quotes = _fetch_spark([_sym(k) for k in keys])
    print(f"[zq] spark {len(quotes)}/{len(keys)} 수신")

    missing = [k for k in keys if _sym(k) not in quotes]
    for k in missing[:4]:  # 폴백은 근월 위주로 제한 (레이트리밋 회피)
        got = _fetch_chart(_sym(k))
        if got:
            quotes[_sym(k)] = got
        time.sleep(1.0)

    implied: Dict[Tuple[int, int], float] = {}
    stamps: List[int] = []
    for k in keys:
        got = quotes.get(_sym(k))
        if got:
            implied[k] = round(100.0 - got[0], 6)
            if got[1]:
                stamps.append(got[1])
    if len(implied) < 3:
        raise RuntimeError(f"ZQ 월물 시세 부족 ({len(implied)}건) — 데이터 소스 점검 필요")
    quote_date = (dt.datetime.fromtimestamp(max(stamps), dt.timezone.utc).date().isoformat()
                  if stamps else dt.date.today().isoformat())
    return implied, quote_date


# ────────────────────────────── 이력 ──────────────────────────────

def save_json(path: str, obj) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def load_history() -> Dict[str, dict]:
    if not os.path.exists(HISTORY_PATH):
        return {}
    try:
        with open(HISTORY_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def snapshot(outlooks: List[eng.MeetingOutlook]) -> dict:
    """이력에 남길 압축 스냅샷: 회의별 누적 조정횟수 + 주요 시나리오 확률."""
    return {
        o.date.isoformat(): {
            "cum": round(o.cum_moves, 4),
            "p": {str(k): round(v, 4) for k, v in o.scenarios},
        } for o in outlooks
    }


def lookup_prior(history: Dict[str, dict], asof: str, days: int) -> dict | None:
    """asof 기준 days일 전 이하의 가장 가까운 스냅샷."""
    target = dt.date.fromisoformat(asof) - dt.timedelta(days=days)
    cands = [d for d in history if d <= target.isoformat()]
    return history[max(cands)] if cands else None


def delta_pp(cur: float, prior: dict | None, mdate: str, k: int) -> str:
    if not prior:
        return ""
    node = prior.get(mdate)
    if not node:
        return ""
    before = node.get("p", {}).get(str(k))
    if before is None:
        return ""
    diff = (cur - float(before)) * 100
    if abs(diff) < 0.5:
        return " (—)"
    return f" ({'▲' if diff > 0 else '▼'}{abs(diff):.0f}%p)"


# ────────────────────────────── 메시지 ──────────────────────────────

WEEKDAY_KR = ["월", "화", "수", "목", "금", "토", "일"]


def build_message(policy: dict, outlooks: List[eng.MeetingOutlook], diag: dict,
                  quote_date: str, history: Dict[str, dict], cal_source: str) -> str:
    lo, hi = policy["target_lo"], policy["target_hi"]
    today = dt.date.today()
    prior_1d = lookup_prior(history, quote_date, 1)
    prior_1w = lookup_prior(history, quote_date, 7)

    lines = [
        "🏛️ <b>CME FedWatch 레이더</b>",
        f"{today.isoformat()} ({WEEKDAY_KR[today.weekday()]}) · 시세 기준 {quote_date}",
        f"현 정책금리 <b>{lo:.2f}~{hi:.2f}%</b> · EFFR {policy['effr']:.2f}%",
        "",
    ]

    for o in outlooks:
        dday = (o.date - today).days
        head = f"<b>{o.date.month}/{o.date.day} FOMC</b> (D-{dday})" if dday > 0 \
            else f"<b>{o.date.month}/{o.date.day} FOMC</b> (당일)"
        if o.confidence == "LOW":
            head += " ⚠️"
        lines.append(head)
        top = sorted(o.scenarios, key=lambda kv: kv[1], reverse=True)[:3]
        for k, p in sorted(top, key=lambda kv: kv[0]):
            if p < 0.01:
                continue
            mark = "▪️" if k == 0 else ("🔺" if k > 0 else "🔻")
            d1 = delta_pp(p, prior_1d, o.date.isoformat(), k)
            lines.append(
                f"  {mark} {eng.move_label(k):<9} {eng.range_label(k, lo, hi)}"
                f"  <b>{p * 100:4.1f}%</b>{d1}")
        lines.append(f"  └ 내재금리 {o.r_post:.3f}% · 누적 {o.cum_moves:+.2f}회")
        lines.append("")

    last = outlooks[-1]
    lines.append("📈 <b>경로</b>")
    lines.append(
        f"  {last.date.year}.{last.date.month}월까지 25bp "
        f"{'인상' if last.cum_moves >= 0 else '인하'} "
        f"<b>{abs(last.cum_moves):.2f}회</b> 반영 (내재 {last.r_post:.2f}%)")
    lines.append("")
    lines.append("🧭 <b>근시일 관측</b>")
    lines.extend(f"  · {s}" for s in commentary(outlooks, prior_1d, prior_1w, today))

    foot = [f"일정: {cal_source}", f"EFFR-중값 스프레드 {diag['spread_bp']:+.1f}bp"]
    if diag.get("anchor_gap_bp") is not None:
        foot.append(f"앵커오차 {diag['anchor_gap_bp']:+.1f}bp")
    lines.append("")
    lines.append(f"<i>{' · '.join(foot)}</i>")
    lines.append("<i>ZQ 선물 정산가 기반 자체 산출 — CME QuikStrike 실시간값과 수%p 차이 가능</i>")
    return "\n".join(lines)


def commentary(outlooks: List[eng.MeetingOutlook], prior_1d: dict | None,
               prior_1w: dict | None, today: dt.date) -> List[str]:
    """규칙 기반 관측 코멘트. 단정하지 않고 관측된 정황만 서술한다."""
    out: List[str] = []
    nxt = outlooks[0]
    probs = dict(nxt.scenarios)
    p_hold = probs.get(0, 0.0)
    p_hike = sum(v for k, v in probs.items() if k > 0)
    p_cut = sum(v for k, v in probs.items() if k < 0)

    if p_hold >= 0.85:
        out.append(f"차기 회의는 동결이 지배적 시나리오({p_hold * 100:.0f}%) — 성명서 문구가 변수")
    elif p_hold >= 0.6:
        out.append(f"차기 회의 동결 우위({p_hold * 100:.0f}%)이나 변경 여지 상존")
    else:
        side = "인상" if p_hike > p_cut else "인하"
        out.append(f"차기 회의 판단 분산 — 동결 {p_hold * 100:.0f}% vs {side} "
                   f"{max(p_hike, p_cut) * 100:.0f}%, 지표 한 건에 크게 흔들리는 구간")

    # 방향성 드리프트
    def cum_of(prior, key):
        node = (prior or {}).get(key)
        return None if not node else float(node.get("cum", 0.0))

    key = nxt.date.isoformat()
    c1w = cum_of(prior_1w, key)
    if c1w is not None:
        drift = nxt.cum_moves - c1w
        if abs(drift) >= 0.08:
            out.append(f"1주간 시장 내재경로가 {'매파' if drift > 0 else '비둘기'} 방향으로 "
                       f"{abs(drift) * 25:.0f}bp 이동")
        else:
            out.append("1주간 내재경로 변화 미미 — 가격에 새로 반영된 정보 제한적")

    # 곡선 형태
    if len(outlooks) >= 2:
        far = outlooks[-1].cum_moves
        near = nxt.cum_moves
        if abs(far - near) >= 0.5:
            out.append(f"근월은 관망, 원월에 조정 {abs(far - near):.1f}회분이 몰린 형태 — "
                       "시점 이연형 프라이싱")

    if any(o.confidence == "LOW" for o in outlooks):
        out.append("⚠️ 표시 회의는 월말 근접으로 계산 민감도가 큼 — 참고용으로만")

    # 캘린더 촉매(규칙 유도만 — 첫째 주 금요일 고용보고서)
    d = dt.date(today.year, today.month, 1)
    while d.weekday() != 4:
        d += dt.timedelta(days=1)
        
    if d < today:
        nm = dt.date(today.year + (today.month == 12), today.month % 12 + 1, 1)
        while nm.weekday() != 4:
            nm += dt.timedelta(days=1)
        d = nm
    out.append(f"다음 촉매: 고용보고서(통상 {d.month}/{d.day}) → CPI·PCE → "
               f"{nxt.date.month}/{nxt.date.day} FOMC")
    return out


def send_telegram(text: str, token: str, chat_id: str) -> None:
    if not token or not chat_id:
        print("[telegram] 자격증명 없음 - 발송 생략")
        print(text)
        return
    r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      json={"chat_id": chat_id, "text": text,
                            "parse_mode": "HTML", "disable_web_page_preview": True},
                      timeout=25)
    r.raise_for_status()
    print("[telegram] 발송 완료")


# ────────────────────────────── 실행 ──────────────────────────────

def main(argv: List[str]) -> int:
    cfg = load_config()
    today = dt.date.today()

    policy = fetch_policy_rate()
    print(f"[policy] {policy}")

    all_meetings, cal_source = load_meetings()
    targets = upcoming(all_meetings, today, MEETINGS_SHOWN)
    if not targets:
        raise RuntimeError("예정된 FOMC 회의를 찾지 못했습니다.")
    print(f"[fomc] {cal_source} / 대상 {[d.isoformat() for d in targets]}")

    implied, quote_date = fetch_zq_curve(today, HORIZON_MONTHS)
    print(f"[zq] 확보 {len(implied)}개 월물 / 시세일 {quote_date}")

    outlooks, diag = eng.compute(
        targets, implied, policy["effr"],
        policy["target_lo"], policy["target_hi"], all_meetings)
    if not outlooks:
        raise RuntimeError("계산 가능한 회의가 없습니다 (월물 데이터 부족).")

    history = load_history()
    message = build_message(policy, outlooks, diag, quote_date, history, cal_source)

    send_telegram(message, cfg["telegram_token"], cfg["telegram_chat_id"])

    # 이력 갱신 — 같은 시세일이 이미 있고 내용이 같으면 파일을 건드리지 않는다
    snap = snapshot(outlooks)
    if history.get(quote_date) != snap:
        history[quote_date] = snap
        for old in sorted(history)[:-HISTORY_KEEP]:
            history.pop(old, None)
        save_json(HISTORY_PATH, dict(sorted(history.items())))
        print(f"[data] history 갱신 ({quote_date})")
    else:
        print("[data] 변경 없음 - history 유지")

    latest = {
        "quote_date": quote_date,
        "policy": policy,
        "calendar_source": cal_source,
        "diagnostics": diag,
        "meetings": [
            {"date": o.date.isoformat(), "method": o.method, "confidence": o.confidence,
             "implied_rate": round(o.r_post, 4), "cum_moves": round(o.cum_moves, 4),
             "scenarios": {str(k): round(v, 4) for k, v in o.scenarios}}
            for o in outlooks
        ],
    }
    if _read_json(LATEST_PATH) != latest:
        save_json(LATEST_PATH, latest)
        print("[data] latest 갱신")
    else:
        print("[data] latest 변경 없음")
    return 0


def _read_json(path: str):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
