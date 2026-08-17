"""
메모리 반도체 4사 포워드 P/E 수집기.

Yahoo Finance(yfinance)에서 forward P/E를 수집해 data.json에 일별 관측치로 누적한다.
- 러너 IP 차단/일시적 오류에 대비해 지수 백오프 재시도
- 개별 종목이 실패해도 전체 실행을 중단하지 않고, 직전 관측치를 carry-forward 하며 stale 표시
- 2026-05까지의 기존 월별 값은 legacy 블록으로 보존 (수집기가 건드리지 않음)

python collect.py            # 수집 후 data.json 갱신
python collect.py --dry-run  # 파일 쓰기 없이 결과만 출력
"""

import argparse
import datetime as dt
import json
import os
import random
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA_PATH = os.path.join(ROOT, "data.json")

KST = dt.timezone(dt.timedelta(hours=9))

TICKERS = [
    {"key": "005930.KS", "name": "삼성전자", "label": "005930.KS", "color": "#5b8cff"},
    {"key": "000660.KS", "name": "SK하이닉스", "label": "000660.KS", "color": "#3ecf8e"},
    {"key": "MU", "name": "마이크론", "label": "NASDAQ:MU", "color": "#ffb454"},
    {"key": "SNDK", "name": "샌디스크", "label": "NASDAQ:SNDK", "color": "#ff6b9d"},
]

# 2026-05까지 확보돼 있던 기존 시계열 (수집기가 수정하지 않는 고정 블록)
LEGACY = {
    "months": ["25.11", "25.12", "26.01", "26.02", "26.03", "26.04", "26.05"],
    "series": {
        "005930.KS": [7.2, 7.6, 7.9, 8.08, 6.9, 5.70, 6.77],
        "000660.KS": [4.8, 5.0, 5.2, 5.28, 5.0, 4.66, 6.79],
        "MU": [12.0, 11.5, 11.0, 10.5, 10.7, 10.2, 9.70],
        "SNDK": [13.5, 13.0, 12.5, 12.2, 12.0, 11.8, 11.68],
    },
    "note": (
        "2025.11~2026.05 구간은 자동 수집 도입 이전의 값으로, 공시·시세 흐름 기반 추정치가 "
        "일부 포함돼 있습니다. 2026.08 이후는 Yahoo Finance 자동 수집치입니다."
    ),
}

# 명백한 오염값 차단용 범위 (포워드 P/E가 이 밖으로 나오면 수집 실패로 간주)
SANE_MIN, SANE_MAX = 0.5, 200.0

MAX_ATTEMPTS = 4
BASE_BACKOFF = 3.0


def log(msg):
    print(f"[collect] {msg}", flush=True)


def fetch_one(symbol):
    """단일 종목의 forward P/E를 조회한다. 실패 시 None."""
    import yfinance as yf

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            info = yf.Ticker(symbol).info or {}
            pe = info.get("forwardPE")
            if pe is None:
                price = info.get("currentPrice") or info.get("regularMarketPrice")
                eps = info.get("forwardEps")
                if price and eps and eps > 0:
                    pe = price / eps
            if pe is None:
                raise ValueError("forwardPE 없음")
            pe = float(pe)
            if not (SANE_MIN <= pe <= SANE_MAX):
                raise ValueError(f"범위 이탈: {pe}")
            return round(pe, 2)
        except Exception as e:  # noqa: BLE001 - 어떤 실패든 재시도 대상
            wait = BASE_BACKOFF * (2 ** (attempt - 1)) + random.uniform(0, 1.5)
            log(f"{symbol} 시도 {attempt}/{MAX_ATTEMPTS} 실패: {type(e).__name__}: {e}")
            if attempt < MAX_ATTEMPTS:
                time.sleep(wait)
    return None


def load_data():
    if os.path.exists(DATA_PATH):
        with open(DATA_PATH, encoding="utf-8") as f:
            return json.load(f)
    return {"meta": {}, "tickers": TICKERS, "legacy": LEGACY, "history": []}


def last_known(history, key):
    for entry in reversed(history):
        v = entry.get("values", {}).get(key)
        if v is not None:
            return v
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    data = load_data()
    history = data.get("history", [])

    now = dt.datetime.now(KST)
    today = now.strftime("%Y-%m-%d")

    values, stale = {}, {}
    for i, t in enumerate(TICKERS):
        if i:
            time.sleep(random.uniform(1.0, 2.5))  # 러너 IP 레이트리밋 완화
        pe = fetch_one(t["key"])
        if pe is None:
            fallback = last_known(history, t["key"])
            values[t["key"]] = fallback
            stale[t["key"]] = True
            log(f"{t['name']}: 수집 실패 → carry-forward {fallback}")
        else:
            values[t["key"]] = pe
            log(f"{t['name']}: {pe}x")

    ok = sum(1 for k in values if not stale.get(k) and values[k] is not None)
    if ok == 0:
        log("전 종목 수집 실패 — data.json을 변경하지 않고 종료합니다.")
        return 1

    entry = {"date": today, "values": values}
    if stale:
        entry["stale"] = stale

    history = [h for h in history if h.get("date") != today]
    history.append(entry)
    history.sort(key=lambda h: h["date"])

    data["tickers"] = TICKERS
    data["legacy"] = LEGACY
    data["history"] = history
    data["meta"] = {
        "schema": 1,
        "updated_at": now.strftime("%Y-%m-%d %H:%M KST"),
        "source": "Yahoo Finance (yfinance)",
        "collected": ok,
        "total": len(TICKERS),
    }

    if args.dry_run:
        print(json.dumps(entry, ensure_ascii=False, indent=2))
        return 0

    with open(DATA_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    log(f"data.json 갱신 완료 ({ok}/{len(TICKERS)}종목, 이력 {len(history)}건)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
