"""
결측 구간(2026-06, 2026-07) 포워드 P/E 복원 스크립트 — 1회성 도구.

방법: Yahoo의 forward P/E 정의가 `주가 ÷ 차기연도(+1y) EPS 추정치`임을 확인한 뒤,
      eps_trend에 남아 있는 30/60/90일 전 EPS 리비전과 당시 종가를 결합해 재계산한다.
      보간·추정이 아니라 당시 실제 입력값으로 되짚는 방식이다.

python backfill.py --check    # 기존 값과의 정합성만 확인 (파일 미변경)
python backfill.py            # data.json에 복원 관측치 기록
"""

import argparse
import datetime as dt
import json
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA_PATH = os.path.join(ROOT, "data.json")

TICKERS = ["005930.KS", "000660.KS", "MU", "SNDK"]

# eps_trend 컬럼 → 기준 시점(오늘로부터 며칠 전)
ANCHORS = [
    ("90daysAgo", 90),   # 2026-05 구간 — 기존 legacy 값과의 정합성 확인용
    ("60daysAgo", 60),   # 2026-06 결측 보완
    ("30daysAgo", 30),   # 2026-07 결측 보완
]

# 실제로 data.json에 기록할 시점 (90일 전은 검증용이므로 기록하지 않음)
WRITE_COLUMNS = {"60daysAgo", "30daysAgo"}


def reconstruct():
    import yfinance as yf

    today = dt.date.today()
    out = {}          # column -> {"date": ..., "values": {...}}
    verify = {}       # ticker -> (yahoo_fwdPE, 계산값)

    for sym in TICKERS:
        tk = yf.Ticker(sym)
        info = tk.info or {}
        trend = tk.eps_trend

        # 정의 검증: forwardPE == price / (+1y EPS)
        px_now = info.get("currentPrice") or info.get("regularMarketPrice")
        eps_now = float(trend.loc["+1y", "current"])
        verify[sym] = (info.get("forwardPE"), px_now / eps_now if px_now and eps_now else None)

        closes = tk.history(start="2026-04-15", end=str(today + dt.timedelta(days=1)))["Close"]
        closes.index = [d.date() for d in closes.index]

        for col, days in ANCHORS:
            target = today - dt.timedelta(days=days)
            prior = [d for d in closes.index if d <= target]
            if not prior:
                continue
            d = max(prior)
            pe = float(closes[d]) / float(trend.loc["+1y", col])
            slot = out.setdefault(col, {"date": str(target), "values": {}, "asof": {}})
            slot["values"][sym] = round(pe, 2)
            slot["asof"][sym] = str(d)

    return out, verify


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()

    data = json.load(open(DATA_PATH, encoding="utf-8"))
    out, verify = reconstruct()

    print("[검증] forwardPE 정의 재현")
    for sym, (yahoo, calc) in verify.items():
        ok = yahoo is not None and calc is not None and abs(yahoo - calc) < 0.02
        print(f"  {sym:<11} yahoo={yahoo} 계산={calc:.4f} 일치={ok}")

    print("\n[복원 결과]")
    for col, _ in ANCHORS:
        slot = out.get(col)
        if not slot:
            continue
        mark = "기록" if col in WRITE_COLUMNS else "검증용(미기록)"
        print(f"  {slot['date']} ({col}, {mark}) → {slot['values']}")

    lg = data.get("legacy", {})
    if lg.get("months"):
        print(f"\n[정합성] legacy 마지막 월 {lg['months'][-1]} 기존값 "
              f"{ {k: v[-1] for k, v in lg['series'].items()} }")

    if args.check:
        return 0

    history = data.get("history", [])
    for col, _ in ANCHORS:
        if col not in WRITE_COLUMNS:
            continue
        slot = out.get(col)
        if not slot:
            continue
        entry = {
            "date": slot["date"],
            "values": slot["values"],
            "method": "reconstructed",
            "asof": slot["asof"],
        }
        history = [h for h in history if h.get("date") != entry["date"]]
        history.append(entry)

    history.sort(key=lambda h: h["date"])
    data["history"] = history
    json.dump(data, open(DATA_PATH, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"\ndata.json 갱신 완료 — 이력 {len(history)}건")
    return 0


if __name__ == "__main__":
    sys.exit(main())
