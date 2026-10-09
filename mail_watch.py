"""세일 시작 메일 감지 — Gmail(IMAP, 읽기 전용)에서 지정한 샵 발신자의 새 메일 '제목'만 보고,
블랙프라이데이·사이버먼데이 같은 키워드가 있으면 텔레그램·카카오로 알린다(2026-10-10 추가).

범위(최소 권한 원칙): 앱 비밀번호는 Gmail 전체 읽기 권한이라 코드 쪽에서 좁힌다 —
  · 폴더 선택은 읽기 전용(readonly), 메일 본문은 가져오지 않는다(BODY.PEEK[HEADER.FIELDS …]만).
  · 발신자 도메인 허용 목록(watchlist.json mail_watch.senders)에 든 메일만 본다.
  · 로그에는 건수만 남긴다 — 저장소가 공개라 Actions 로그가 공개되므로 제목·발신자는 절대 로그에 쓰지 않는다.
    제목은 비공개 채널(텔레그램·카카오)로만 보낸다.
계정: 환경변수 NOTIFY_EMAIL(없으면 ~/.config/whisky-watch/notify_email.txt), 비밀번호: GMAIL_APP_PASSWORD.
"""
import email, email.header, email.utils, imaplib, os, re, time
from datetime import datetime, timedelta
from pathlib import Path

CONF = Path.home() / ".config/whisky-watch"
SHOP = {"thewhiskyexchange.com": "The Whisky Exchange", "masterofmalt.com": "Master of Malt",
        "whiskyshop.com": "The Whisky Shop", "dekanta.com": "Dekanta", "nickollsandperks.co.uk": "Nickolls & Perks",
        "shinanoya": "시나노야", "mukawa-spirit.com": "무카와", "theultimatespirits.jp": "RUDDER",
        "deinwhisky.de": "DeinWhisky", "lmdw.com": "La Maison du Whisky"}
SHOP_URL = {"thewhiskyexchange.com": "https://www.thewhiskyexchange.com/", "masterofmalt.com": "https://www.masterofmalt.com/black-friday",
            "whiskyshop.com": "https://www.whiskyshop.com/black-friday", "dekanta.com": "https://dekanta.com/"}


def _address():
    a = (os.environ.get("NOTIFY_EMAIL") or "").strip()
    if not a:
        try:
            a = (CONF / "notify_email.txt").read_text().strip()
        except FileNotFoundError:
            a = ""
    return a if "@" in a else ""


def _password():
    raw = os.environ.get("GMAIL_APP_PASSWORD")
    if raw is None:
        try:
            raw = (CONF / "gmail_app_password.txt").read_text()
        except FileNotFoundError:
            return ""
    return "".join(raw.split())          # 앱 비밀번호 표시 형태의 NBSP 등 모든 공백 제거


def _dec(s):
    try:
        return str(email.header.make_header(email.header.decode_header(s or "")))
    except Exception:
        return s or ""


def scan(senders, since_days):
    """[(도메인, 날짜(YYYY-MM-DD), 제목, message-id)] — 헤더만, 읽기 전용."""
    addr, pw = _address(), _password()
    if not addr or not pw:
        raise RuntimeError("메일 계정/비밀번호 없음")
    M = imaplib.IMAP4_SSL("imap.gmail.com", timeout=30)
    try:
        M.login(addr, pw)
        folder = None
        for line in M.list()[1]:
            s = line.decode("utf-8", "replace")
            if "\\All" in s:
                folder = s.rsplit(' "/" ', 1)[-1].strip()
        if not folder:
            raise RuntimeError("All Mail 폴더를 못 찾음")
        if M.select(folder if folder.startswith('"') else f'"{folder}"', readonly=True)[0] != "OK":
            raise RuntimeError("폴더 선택 실패")
        since = (datetime.now() - timedelta(days=since_days)).strftime("%d-%b-%Y")
        out = []
        for dom in senders:
            typ, data = M.search(None, "SINCE", since, "FROM", dom)
            ids = data[0].split()[-100:]
            if not ids:
                continue
            typ, msgs = M.fetch(b",".join(ids).decode(), "(BODY.PEEK[HEADER.FIELDS (SUBJECT DATE MESSAGE-ID)])")
            for m in msgs:
                if isinstance(m, tuple):
                    h = email.message_from_bytes(m[1])
                    try:
                        d = email.utils.parsedate_to_datetime(h["Date"]).strftime("%Y-%m-%d")
                    except Exception:
                        d = ""
                    out.append((dom, d, _dec(h["Subject"]).strip(), (h["Message-ID"] or "").strip()))
        return out
    finally:
        try:
            M.logout()
        except Exception:
            pass


def _in_window(cfg, now):
    md = now.strftime("%m-%d")
    return cfg.get("from", "01-01") <= md <= cfg.get("to", "12-31")


def matches(subject, keywords):
    return any(re.search(k, subject, re.I) for k in keywords)


def run(wl, seen, log, notify_fn, test=False):
    """watchlist.json의 mail_watch 설정대로 한 번 점검. notify_fn(text, url, shop)로 알린다."""
    cfg = wl.get("mail_watch") or {}
    senders, kws = cfg.get("senders", []), cfg.get("keywords", [])
    if not senders or not kws:
        return
    now = datetime.now()
    st = seen.setdefault("mail", {"last": 0, "ids": []})
    if test:
        rows = scan(senders, 30)
        hits = [r for r in rows if matches(r[2], kws)]
        log(f"[TEST] 메일 스캔 성공 — 허용 발신자 30일 {len(rows)}통, 키워드 일치 {len(hits)}통")
        return
    if not _in_window(cfg, now):
        return
    if time.time() - st.get("last", 0) < cfg.get("throttle_min", 30) * 60:
        return
    first = "ids" not in st or st.get("last", 0) == 0
    rows = scan(senders, cfg.get("lookback_days", 3))
    st["last"] = time.time()
    done = set(st.get("ids", []))
    new = [r for r in rows if r[3] and r[3] not in done and matches(r[2], kws)]
    st["ids"] = (list(done) + [r[3] for r in rows if r[3]])[-400:]
    log(f"  [MAIL] 허용 발신자 {len(rows)}통 확인, 새 세일 메일 {len(new)}통" + (" (첫 실행 — 기준선만 기록)" if first else ""))
    if first:
        return
    for dom, d, subj, _ in new[:5]:
        shop = next((v for k, v in SHOP.items() if k in dom), dom)
        url = next((v for k, v in SHOP_URL.items() if k in dom), "https://www.thewhiskyexchange.com/")
        notify_fn(f"📬 세일 메일 도착 · {shop}\n{subj}\n→ 대조하려면 Claude에게 'TWE 대조해줘'", url, shop)
