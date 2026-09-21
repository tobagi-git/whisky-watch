"""텔레그램 봇 최소 클라이언트 (urllib만 사용). whisky_watch.py·bottle_tracker.py 공용.

토큰: 환경변수 TELEGRAM_BOT_TOKEN (GitHub Secret). chat_id는 사용자가 봇에 /start를 보내면
bottle_tracker가 getUpdates로 알아내 data/tracker_state.json에 저장한다(환경변수
TELEGRAM_CHAT_ID로 덮어쓸 수도 있음).
"""
import json, os, urllib.request, urllib.parse

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")


def api(method, **params):
    if not TOKEN:
        return None
    url = f"https://api.telegram.org/bot{TOKEN}/{method}"
    data = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None}).encode()
    req = urllib.request.Request(url, data=data)
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode())


def send(chat_id, text, html=True):
    """4096자 제한 — 넘으면 잘라서 여러 번 보낸다."""
    if not TOKEN or not chat_id:
        return False
    chunks, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > 3800:
            chunks.append(cur)
            cur = ""
        cur += line + "\n"
    chunks.append(cur)
    for c in chunks:
        api("sendMessage", chat_id=chat_id, text=c,
            parse_mode="HTML" if html else None, disable_web_page_preview="true")
    return True


def get_updates(offset=None):
    r = api("getUpdates", offset=offset, timeout=0)
    return (r or {}).get("result", [])


def esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
