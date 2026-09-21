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
    clean = {}
    for k, v in params.items():
        if v is None:
            continue
        clean[k] = json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v
    req = urllib.request.Request(url, data=urllib.parse.urlencode(clean).encode())
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return {"ok": False, "error": e.read().decode(errors="replace")[:200]}


def send(chat_id, text, html=True, reply_markup=None):
    """4096자 제한 — 넘으면 잘라서 여러 번 보낸다. 버튼(reply_markup)은 마지막 조각에만."""
    if not TOKEN or not chat_id:
        return None
    chunks, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > 3800:
            chunks.append(cur)
            cur = ""
        cur += line + "\n"
    chunks.append(cur)
    last = None
    for i, c in enumerate(chunks):
        last = api("sendMessage", chat_id=chat_id, text=c,
                   parse_mode="HTML" if html else None, disable_web_page_preview="true",
                   reply_markup=reply_markup if i == len(chunks) - 1 else None)
    return last


def edit(chat_id, message_id, text, reply_markup=None):
    if not message_id:
        return send(chat_id, text, reply_markup=reply_markup)
    r = api("editMessageText", chat_id=chat_id, message_id=message_id, text=text[:4000],
            parse_mode="HTML", disable_web_page_preview="true", reply_markup=reply_markup)
    if not (r or {}).get("ok"):
        return send(chat_id, text, reply_markup=reply_markup)
    return r


def answer_callback(callback_id, text=None):
    return api("answerCallbackQuery", callback_query_id=callback_id, text=text)


def get_updates(offset=None):
    r = api("getUpdates", offset=offset, timeout=0)
    return (r or {}).get("result", [])


def esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
