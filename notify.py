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

def main():
    c = cfg()
    kst = datetime.datetime.utcnow() + datetime.timedelta(hours=9)
    msg = (
        f"📊 <b>메모리 반도체 4사 포워드 P/E</b>\n"
        f"🗓 {kst:%Y-%m-%d (%a)} 평일 06:00 KST\n\n"
        f"삼성전자 · SK하이닉스 · 마이크론 · 샌디스크\n"
        f"추세·현재값 대시보드를 확인하세요 👇\n"
        f"{URL}"
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
