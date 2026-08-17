"""
data.json을 읽어 메모리 반도체 4사 포워드 P/E 텔레그램 브리프를 발송한다.
값과 요약 문구를 모두 수집 결과에서 파생시키므로 하드코딩이 없다.

python notify.py            # 발송
python notify.py --dry-run  # 발송 없이 메시지만 출력
"""

import argparse
import json
import os
import sys

import requests

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA_PATH = os.path.join(ROOT, "data.json")
URL = "https://jinhae8971.github.io/forward-pe-dashboard/"


def cfg():
    c = {
        "telegram_token": os.environ.get("TELEGRAM_TOKEN", ""),
        "telegram_chat_id": os.environ.get("TELEGRAM_CHAT_ID", ""),
    }
    p = os.path.join(ROOT, "config.json")
    if os.path.exists(p):
        for k, v in json.load(open(p, encoding="utf-8")).items():
            c[k] = c.get(k) or v
    return c


def prev_observation(history, key, cur_date):
    """직전 관측일의 값 (없으면 None)."""
    for h in reversed(history[:-1]):
        if h["date"] != cur_date and h.get("values", {}).get(key) is not None:
            return h["values"][key], h["date"]
    return None, None


def legacy_last(data, key):
    lg = data.get("legacy", {})
    arr = lg.get("series", {}).get(key)
    return (arr[-1], lg.get("months", ["-"])[-1]) if arr else (None, None)


def build_summary(data, rows):
    """규칙 기반 요약 — 데이터에서 파생, 문구 하드코딩 없음."""
    lines = []
    valid = [r for r in rows if r["cur"] is not None]
    if not valid:
        return ["· 유효한 수집값이 없습니다"]

    lo = min(valid, key=lambda r: r["cur"])
    hi = max(valid, key=lambda r: r["cur"])
    lines.append(
        f"· 최저 {lo['name']} {lo['cur']:.2f}x · 최고 {hi['name']} {hi['cur']:.2f}x "
        f"(격차 {hi['cur'] - lo['cur']:.2f}x)"
    )

    moved = [r for r in valid if r["diff"] is not None and abs(r["diff"]) >= 0.01]
    ups = [r for r in moved if r["diff"] > 0]
    downs = [r for r in moved if r["diff"] < 0]
    if not moved:
        lines.append("· 직전 수집 대비 유의미한 변동 없음")
    elif ups and not downs:
        lines.append(f"· 4사 중 {len(ups)}개 종목 일제히 밸류에이션 상승")
    elif downs and not ups:
        lines.append(f"· 4사 중 {len(downs)}개 종목 일제히 밸류에이션 하락")
    else:
        big = max(moved, key=lambda r: abs(r["pct"]))
        lines.append(
            f"· 상승 {len(ups)} / 하락 {len(downs)} · 변동 폭 최대는 "
            f"{big['name']} ({big['pct']:+.1f}%)"
        )

    # 국내 2사 상대 밸류에이션
    ss = next((r for r in valid if r["key"] == "005930.KS"), None)
    sk = next((r for r in valid if r["key"] == "000660.KS"), None)
    if ss and sk:
        if sk["cur"] > ss["cur"]:
            lines.append(f"· SK하이닉스가 삼성전자보다 {sk['cur'] - ss['cur']:.2f}x 높은 구간")
        elif ss["cur"] > sk["cur"]:
            lines.append(f"· 삼성전자가 SK하이닉스보다 {ss['cur'] - sk['cur']:.2f}x 높은 구간")
        else:
            lines.append("· 삼성전자·SK하이닉스 포워드 PER 동일 수준")

    stale = [r["name"] for r in rows if r["stale"]]
    if stale:
        lines.append(f"· ⚠️ 수집 실패로 직전값 유지: {', '.join(stale)}")
    return lines


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if not os.path.exists(DATA_PATH):
        print("data.json이 없습니다. collect.py를 먼저 실행하세요.", file=sys.stderr)
        return 1

    data = json.load(open(DATA_PATH, encoding="utf-8"))
    history = data.get("history", [])
    if not history:
        print("수집 이력이 비어 있습니다.", file=sys.stderr)
        return 1

    last = history[-1]
    meta = data.get("meta", {})

    rows = []
    for t in data["tickers"]:
        cur = last["values"].get(t["key"])
        prev, prev_date = prev_observation(history, t["key"], last["date"])
        base_label = f"{prev_date[5:]} 대비" if prev_date else None
        if prev is None:
            prev, m = legacy_last(data, t["key"])
            base_label = f"{m} 대비" if m else None
        diff = pct = None
        if cur is not None and prev:
            diff = cur - prev
            pct = diff / prev * 100
        rows.append({
            "key": t["key"], "name": t["name"], "label": t["label"],
            "cur": cur, "diff": diff, "pct": pct, "base": base_label,
            "stale": bool(last.get("stale", {}).get(t["key"])),
        })

    lines = []
    for r in rows:
        if r["cur"] is None:
            lines.append(f"• <b>{r['name']}</b> ({r['label']}) : 수집 실패")
            continue
        if r["diff"] is None:
            tail = ""
        else:
            arrow = "🔺" if r["diff"] > 0 else ("🔻" if r["diff"] < 0 else "▪️")
            tail = f" {arrow}{abs(r['diff']):.2f} ({r['pct']:+.1f}%)"
        mark = " ⚠️" if r["stale"] else ""
        lines.append(f"• <b>{r['name']}</b> ({r['label']}) : <b>{r['cur']:.2f}x</b>{tail}{mark}")

    base = next((r["base"] for r in rows if r["base"]), "직전 수집 대비")
    msg = (
        f"📊 <b>메모리 반도체 4사 포워드 P/E</b>\n"
        f"🗓 {meta.get('updated_at', last['date'])} · 기준 {base}\n"
        f"━━━━━━━━━━━━━━━━\n"
        + "\n".join(lines) + "\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"📝 <b>요약</b>\n"
        + "\n".join(build_summary(data, rows)) + "\n\n"
        f"🔗 추세·현재값 대시보드\n{URL}\n"
        f"<i>※ 향후 12개월 예상 EPS 기준 · 출처 {meta.get('source', 'Yahoo Finance')} · 참고용</i>"
    )

    if args.dry_run:
        print(msg)
        return 0

    c = cfg()
    if not c["telegram_token"] or not c["telegram_chat_id"]:
        print("텔레그램 자격 정보가 없습니다.", file=sys.stderr)
        return 1

    r = requests.post(
        f"https://api.telegram.org/bot{c['telegram_token']}/sendMessage",
        json={"chat_id": c["telegram_chat_id"], "text": msg,
              "parse_mode": "HTML", "disable_web_page_preview": False},
        timeout=20,
    )
    r.raise_for_status()
    print("sent:", r.json().get("ok"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
