import os, json, datetime, requests

def cfg():
    c = {
        "telegram_token":   os.environ.get("TELEGRAM_TOKEN", ""),
        "telegram_chat_id": os.environ.get("TELEGRAM_CHAT_ID", ""),
    }
    p = os.path.join(os.path.dirname(__file__), "config.json")
    if os.path.exists(p):
        for k, v in json.load(open(p, encoding="utf-8")).items():
            c[k] = c.get(k) or v
    return c

URL = "https://jinhae8971.github.io/forward-pe-dashboard/"

# 최신 포워드 P/E (2026-05 기준) — 전월(2026-04) 대비
DATA = [
    ("삼성전자",   "005930",  6.77, 5.70),
    ("SK하이닉스", "000660",  6.79, 4.66),
    ("마이크론",   "MU",      9.70, 10.20),
    ("샌디스크",   "SNDK",    11.68, 11.80),
]

def main():
    c = cfg()
    kst = datetime.datetime.utcnow() + datetime.timedelta(hours=9)

    lines = []
    for name, tic, cur, prev in DATA:
        diff = cur - prev
        arrow = "🔺" if diff > 0 else ("🔻" if diff < 0 else "▪️")
        lines.append(f"• <b>{name}</b> ({tic}) : <b>{cur:.2f}x</b> {arrow}{abs(diff):.2f}")
    table = "\n".join(lines)

    msg = (
        f"📊 <b>메모리 반도체 4사 포워드 P/E</b>\n"
        f"🗓 {kst:%Y-%m-%d (%a)} · 06:00 KST\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"{table}\n"
        f"━━━━━━━━━━━━━━━━\n"
        f"📝 <b>요약</b>\n"
        f"· SK하이닉스가 삼성전자를 포워드 PER에서 첫 추월(6.79x vs 6.77x)\n"
        f"· 국내 2사는 저점 대비 반등, 마이크론·샌디스크는 하향 안정세\n"
        f"· 4사 모두 업종 중앙값(약 23x) 대비 큰 폭 할인 구간\n\n"
        f"🔗 추세·현재값 대시보드\n{URL}\n"
        f"<i>※ 향후 12개월 예상 EPS 기준, 출처별 편차 있음 · 참고용</i>"
    )
    r = requests.post(
        f"https://api.telegram.org/bot{c['telegram_token']}/sendMessage",
        json={"chat_id": c["telegram_chat_id"], "text": msg,
              "parse_mode": "HTML", "disable_web_page_preview": False},
        timeout=20,
    )
    r.raise_for_status()
    print("sent:", r.json().get("ok"))

if __name__ == "__main__":
    main()
