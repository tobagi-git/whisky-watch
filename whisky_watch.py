#!/usr/bin/env python3
"""
whisky_watch.py — 위스키 온라인숍 5곳을 폴링해 신규 매물·세일을 인박스에 쌓는다 (토큰 0)

launchd(com.ruby.whisky-watch)가 하루 여러 차례 호출한다. Claude를 거치지 않으므로 토큰이 들지 않는다.
분석은 하지 않는다 — "무엇이 새로 나왔나 / 얼마나 싸졌나"만 수집한다 (dumb collector, smart reader).
읽고 취향 매칭·세금 환산·심층 분석하는 것은 whisky 스킬이 인박스를 읽고 브리핑할 때 한다
(같은 구조: corp-monitor의 dart_watch.py를 그대로 본떴다).

감시 대상 (2026-07-18 검증 완료, requests만으로 전부 가능 — Cloudflare 없음):
  - RUDDER (theultimatespirits.jp)   : Shopify products.json API, 10개 컬렉션
  - Mukawa (mukawa-spirit.com)       : 신착상품 페이지, ⚠️ EUC-JP 인코딩
  - DeinWhisky (deinwhisky.de)       : Neues/Angebote/Raritäten/Exklusiv, 자체 세일 배지 있음
  - Shinanoya (shinanoya-tokyo.jp)   : 신착(ct211)/決算セール(ct1443) 카테고리
  - Vitalaus (vitalaus.com)          : 아메리칸/스카치/주류특가 리스트

Master of Malt·The Whisky Exchange는 Cloudflare로 막혀 순수 스크립트로 불가 —
그쪽은 계속 Claude 세션(whisky-rare-monitor 스케줄 작업)이 브라우저로 확인한다.

사용법:
  python3 whisky_watch.py            # 폴링 (6시간 스로틀 — launchd용)
  python3 whisky_watch.py --force    # 스로틀 무시하고 즉시 폴링
  python3 whisky_watch.py --status   # 상태 요약 (마지막 실행·인박스 건수·사이트별 보유 수)

파일:
  워치리스트  ~/.claude/skills/whisky/watchlist.json          (사이트·키워드 설정)
  인박스      ~/Claude/Projects/위스키/모니터링/inbox.md       (whisky 스킬 브리핑이 읽음)
  상태        ~/Claude/Projects/위스키/모니터링/seen.json      (상품ID→마지막 가격, 중복/세일 판정)
  로그        ~/Claude/Projects/위스키/모니터링/watch.log
"""
import sys, re, json, time, html, urllib.error, urllib.request
from datetime import datetime, timedelta
from pathlib import Path

import os
SKILL = Path.home() / ".claude/skills/whisky"
# 클라우드(GitHub Actions)에서는 환경변수로 경로를 덮어쓴다 — 로컬 기본값은 그대로.
WATCHLIST = Path(os.environ.get("WHISKY_WATCHLIST", SKILL / "watchlist.json"))
DATA = Path(os.environ.get("WHISKY_DATA_DIR", Path.home() / "Claude/Projects/위스키/모니터링"))
INBOX = DATA / "inbox.md"
SEEN = DATA / "seen.json"
LOG = DATA / "watch.log"
THROTTLE_H = 0.15  # 9분 — launchd 중복 기동만 걸러내는 용도(2026-09-18 이전: 6시간이라 5시간
                    # 간격의 스케줄이 대부분 스킵되는 버그가 있었음, 실측 로그로 확인 후 수정)

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"

# Gmail 앱 비밀번호 — 절대 ~/Claude 아래에 두지 않는다(백업 트리라 유출 경로가 됨). ~/.config가 표준 위치.
GMAIL_APP_PASSWORD_FILE = Path.home() / ".config/whisky-watch/gmail_app_password.txt"


def log(msg):
    DATA.mkdir(parents=True, exist_ok=True)
    line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  {msg}"
    with LOG.open("a") as f:
        f.write(line + "\n")
    print(line)


def load_json(path, default):
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def fetch(url, timeout=20, encoding=None):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
    if encoding:
        return raw.decode(encoding, errors="replace")
    return raw.decode("utf-8", errors="replace")


def matches_keyword(title, kw_list):
    t = title.lower()
    for k in kw_list:
        if k.lower() in t:
            return k
    return None


# ---------------------------------------------------------------- RUDDER
def scan_rudder(cfg, kws):
    items = []
    for handle in cfg.get("collections", []):
        url = f"https://theultimatespirits.jp/collections/{handle}/products.json?limit=250"
        try:
            data = json.loads(fetch(url))
        except Exception as e:
            log(f"  [ERR] rudder/{handle}: {e}")
            continue
        for p in data.get("products", []):
            v = (p.get("variants") or [{}])[0]
            price = v.get("price")
            compare = v.get("compare_at_price")
            on_sale = bool(compare) and price and float(compare) > float(price)
            items.append({
                "site": "rudder", "id": f"rudder:{p['id']}", "title": p.get("title", ""),
                "url": f"https://theultimatespirits.jp/products/{p.get('handle', '')}",
                "price": price, "on_sale_flag": on_sale,
                "available": any(x.get("available") for x in (p.get("variants") or [])),
                "hint": p.get("product_type") or "",
            })
        time.sleep(0.3)
    return items


# ---------------------------------------------------------------- Shopify 공용 (영국·독일 샵)
def scan_shopify(cfg, kws):
    """Shopify 샵의 최신 게시 상품 N페이지. products.json은 게시일 최신순이라 앞쪽만 보면 신착이 잡힌다.
    ⚠️ 가격은 반드시 products.json(샵 기준 통화)에서 읽는다 — 검색 API·쿠키 기반 가격은 접속 국가에
    따라 KRW·USD 등으로 바뀐다(Shopify Markets, 2026-09-25 실측: 같은 상품이 £97.96 / ₩149,753)."""
    site, base = cfg["_site"], cfg["base"].rstrip("/")
    items = []
    pages = int(cfg.get("deep_pages", 40)) if cfg.get("_deep") else int(cfg.get("pages", 2))
    for page in range(1, pages + 1):
        data = None
        for attempt in range(4):
            try:
                data = json.loads(fetch(f"{base}/products.json?limit=250&page={page}"))
                break
            except urllib.error.HTTPError as e:
                # Shopify는 페이지를 연달아 긁으면 503/429로 막는다(정밀 스캔 때 실측) — 물러섰다 재시도
                if e.code in (429, 503) and attempt < 3:
                    time.sleep(5 * (attempt + 1))
                    continue
                log(f"  [ERR] {site}/p{page}: {e}")
                break
            except Exception as e:
                log(f"  [ERR] {site}/p{page}: {e}")
                break
        if data is None:
            break
        prods = data.get("products", [])
        for p in prods:
            vs = p.get("variants") or [{}]
            v = vs[0]
            price, compare = v.get("price"), v.get("compare_at_price")
            items.append({
                "site": site, "id": f"{site}:{p['id']}", "title": p.get("title", ""),
                "url": f"{base}/products/{p.get('handle', '')}",
                "price": price, "cur": cfg.get("cur"),
                "available": any(x.get("available") for x in vs),
                "on_sale_flag": bool(compare) and bool(price) and float(compare) > float(price),
                "hint": (p.get("product_type") or "") + "|" + " ".join(list(p.get("tags") or [])[:8]),
            })
        if len(prods) < 250:
            break
        time.sleep(1.0 if cfg.get("_deep") else 0.3)
    return items


# ---------------------------------------------------------------- HTFW (신착 페이지)
def scan_htfw(cfg, kws):
    import sources
    items = []
    for page in range(1, int(cfg.get("pages", 2)) + 1):
        try:
            got = sources.htfw_parse(fetch(f"https://www.htfw.com/new-arrivals?p={page}"))
        except Exception as e:
            log(f"  [ERR] htfw/p{page}: {e}")
            break
        for it in got:
            items.append({"site": "htfw", "id": f"htfw:{it['id']}", "title": it["title"], "url": it["url"],
                          "price": it["price"], "cur": "GBP", "available": it["live"], "on_sale_flag": False,
                          "hint": it.get("hint", "")})
        if not got:
            break
        time.sleep(0.5)
    return items


# ---------------------------------------------------------------- Whisky-Maniac (NEW IN)
def scan_whiskymaniac(cfg, kws):
    import sources
    got = sources.whiskymaniac_parse(fetch(f"https://www.whisky-maniac.de/c/new-in?page={int(cfg.get('pages', 2))}"))
    return [{"site": "whiskymaniac", "id": f"whiskymaniac:{it['id']}", "title": it["title"], "url": it["url"],
             "price": it["price"], "cur": "EUR", "available": it["live"], "on_sale_flag": False} for it in got]


# ---------------------------------------------------------------- Nickolls & Perks (위스키 카테고리 최신순)
def scan_nickolls(cfg, kws):
    import sources
    pages = int(cfg.get("deep_pages", 40)) if cfg.get("_deep") else 1
    out = []
    for page in range(1, pages + 1):
        got = sources.nickolls_items({"category": cfg.get("category", 6723), "orderby": "date", "order": "desc",
                                      "per_page": int(cfg.get("per_page", 100)), "page": page})
        out += [{"site": "nickolls", "id": f"nickolls:{it['id']}", "title": it["title"], "url": it["url"],
                 "price": it["price"], "cur": it["cur"], "available": it["live"], "on_sale_flag": False,
                 "hint": "whisky"} for it in got]
        if len(got) < int(cfg.get("per_page", 100)):
            break
        time.sleep(0.3)
    return out


# ---------------------------------------------------------------- Whiskysite.nl (카테고리별 최신순, 샘플 제외)
def scan_whiskysite(cfg, kws):
    """전체 최신순은 6cl 샘플이 앞을 다 차지한다(최신 24개 전부 샘플, 2026-09-25) — 위스키 카테고리별로 본다."""
    import sources
    items = []
    for cat in cfg.get("categories", ["scotch-whisky", "independent-bottlers", "rare-old-single-malt-whisky"]):
        try:
            got = sources.whiskysite_items(f"{cat}/", {"sort": "newest"})
        except Exception as e:
            log(f"  [ERR] whiskysite/{cat}: {e}")
            continue
        items += [{"site": "whiskysite", "id": f"whiskysite:{it['id']}", "title": it["title"], "url": it["url"],
                   "price": it["price"], "cur": "EUR", "available": it["live"], "on_sale_flag": False,
                   "hint": "whisky"} for it in got]
        time.sleep(0.5)
    return items


# ---------------------------------------------------------------- La Maison du Whisky (위스키 활성화일 최신순)
def scan_lmdw(cfg, kws):
    import sources
    n = int(cfg.get("page_size", 60))
    pages = int(cfg.get("deep_pages", 20)) if cfg.get("_deep") else 1
    if cfg.get("_deep"):
        n = 200
    out = []
    for page in range(1, pages + 1):
        got = sources.lmdw_items({"f": {"category_uid": {"eq": cfg.get("category_uid", sources.LMDW_WHISKY_UID)}},
                                  "n": n, "p": page, "sort": {"lmdw_activation_date": "DESC"}})
        out += [{"site": "lmdw", "id": f"lmdw:{it['id']}", "title": it["title"], "url": it["url"],
                 "price": it["price"], "cur": it["cur"], "available": it["live"], "on_sale_flag": False,
                 "hint": "whisky"} for it in got]
        if len(got) < n:
            break
        time.sleep(0.3)
    return out


# ---------------------------------------------------------------- Mukawa
def scan_mukawa(cfg, kws):
    url = "https://mukawa-spirit.com/?mode=srh&sort=n"
    try:
        page = fetch(url, encoding="euc-jp")
    except Exception as e:
        log(f"  [ERR] mukawa: {e}")
        return []
    items = []
    # <a href="...pid=XXXX..." ...>제목 ¥가격 ...</a> 형태를 통짜로 훑는다
    for m in re.finditer(r'<a[^>]+href="([^"]*pid=(\d+)[^"]*)"[^>]*>(.*?)</a>', page, re.S):
        href, pid, inner = m.group(1), m.group(2), m.group(3)
        text = re.sub(r"<[^>]+>", " ", inner)
        text = html.unescape(text)  # &yen; 등 HTML 엔티티 해제 (안 하면 가격이 안 잡힘)
        text = re.sub(r"\s+", " ", text).strip()
        if not text:
            continue
        pm = re.search(r"[¥￥]\s*([\d,]+)", text)
        price = pm.group(1).replace(",", "") if pm else None
        title = re.sub(r"[¥￥].*$", "", text).strip()
        items.append({
            "site": "mukawa", "id": f"mukawa:{pid}", "title": title,
            "url": href if href.startswith("http") else "https://mukawa-spirit.com/" + href.lstrip("/"),
            "price": price, "on_sale_flag": False,
        })
    return items


# ------------------------------------------------------------- DeinWhisky
def scan_deinwhisky(cfg, kws):
    items = []
    for cat in cfg.get("categories", []):
        url = f"https://www.deinwhisky.de/{cat}/"
        try:
            page = fetch(url)
        except Exception as e:
            log(f"  [ERR] deinwhisky/{cat}: {e}")
            continue
        # 블록 단위 파싱: data-ordernumber 마다 다음 블록까지 슬라이스
        idxs = [m.start() for m in re.finditer(r'data-ordernumber="', page)]
        for i, s in enumerate(idxs):
            e = idxs[i + 1] if i + 1 < len(idxs) else s + 3000
            block = page[s:e]
            oid = re.search(r'data-ordernumber="([^"]+)"', block)
            title = re.search(r'class="product--title"[^>]*title="([^"]+)"', block)
            if not oid or not title:
                continue
            price = re.search(r'class="price--default[^"]*">\s*([\d.,]+)\s*&nbsp;&euro;', block)
            discount = "badge--discount" in block
            newcomer = "badge--newcomer" in block
            price_eur = None
            if price:
                # 독일식: "1.234,56" → 1234.56 (천단위 마침표 제거, 콤마→소수점)
                price_eur = price.group(1).replace(".", "").replace(",", ".")
            href = re.search(r'href="([^"]+)"', block)
            items.append({
                "site": "deinwhisky", "id": f"deinwhisky:{oid.group(1)}",
                "title": title.group(1),
                "url": href.group(1) if href else url,
                "price": price_eur,
                "on_sale_flag": discount, "new_flag": newcomer,
            })
        time.sleep(0.3)
    return items


# ------------------------------------------------------------- Shinanoya
def scan_shinanoya(cfg, kws):
    items = []
    for label, cat_id in cfg.get("categories", {}).items():
        url = f"https://shinanoya-tokyo.jp/view/category/{cat_id}"
        try:
            page = fetch(url)
        except Exception as e:
            log(f"  [ERR] shinanoya/{label}: {e}")
            continue
        for m in re.finditer(
            r'/view/item/(\d+)[^"]*".*?class="name">\s*<a[^>]*>\s*([^<]+?)\s*</a>\s*</h3>\s*'
            r'<p class="price"[^>]*>&yen;([\d,]+)',
            page, re.S,
        ):
            iid, title, price = m.group(1), m.group(2).strip(), m.group(3).replace(",", "")
            items.append({
                "site": "shinanoya", "id": f"shinanoya:{iid}", "title": title,
                "url": f"https://shinanoya-tokyo.jp/view/item/{iid}",
                "price": price, "on_sale_flag": (label == "세일"),
            })
        time.sleep(0.3)
    return items


# -------------------------------------------------------------- Vitalaus
def scan_vitalaus(cfg, kws):
    items = []
    for label, list_id in cfg.get("categories", {}).items():
        url = f"https://vitalaus.com/shop/{list_id}"
        try:
            page = fetch(url)
        except Exception as e:
            log(f"  [ERR] vitalaus/{label}: {e}")
            continue
        for m in re.finditer(
            r'<a href="(https://vitalaus\.com/shop/([A-Z0-9]+))">\s*<img[^>]*alt="([^"]+)"',
            page,
        ):
            url_, code, title = m.group(1), m.group(2), m.group(3)
            block_end = page.find("</li>", page.find(code))
            block = page[page.find(code):block_end] if block_end > 0 else ""
            pm = re.search(r"￦\s*([\d,]+)", block)
            price = pm.group(1).replace(",", "") if pm else None
            soldout = "재고없음" in block
            items.append({
                "site": "vitalaus", "id": f"vitalaus:{code}", "title": title,
                "url": url_, "price": price, "on_sale_flag": False, "soldout": soldout,
            })
        time.sleep(0.3)
    # 같은 상품이 여러 카테고리에 중복 등장할 수 있음 → id로 dedup
    return items


def _event(kind, it, kw):
    return {"ts": datetime.now().isoformat(timespec="seconds"), "type": kind, "site": it["site"],
            "id": it["id"], "title": it["title"], "url": it["url"], "price": it.get("price"),
            "cur": it.get("cur"), "available": it.get("available"), "hint": it.get("hint", ""), "kw": kw}


def dedup_merge(items):
    """같은 id가 여러 카테고리 페이지에 걸쳐 나올 수 있다(예: DeinWhisky의 동일 상품이
    Angebote/Raritäten 양쪽에 다른 배지 상태로 렌더링됨 — 2026-07-18 실측).
    on_sale_flag는 OR로 합치고(어느 한쪽이라도 세일 표시하면 신뢰), 나머지는 마지막 값을 쓴다."""
    merged = {}
    for it in items:
        iid = it["id"]
        if iid not in merged:
            merged[iid] = dict(it)
        else:
            merged[iid].update(it)
            merged[iid]["on_sale_flag"] = merged[iid].get("on_sale_flag") or it.get("on_sale_flag")
    return list(merged.values())


SCANNERS = {
    "rudder": scan_rudder,
    "mukawa": scan_mukawa,
    "deinwhisky": scan_deinwhisky,
    "shinanoya": scan_shinanoya,
    "vitalaus": scan_vitalaus,
    "shopify": scan_shopify,
    "htfw": scan_htfw,
    "whiskymaniac": scan_whiskymaniac,
    "nickolls": scan_nickolls,
    "whiskysite": scan_whiskysite,
    "lmdw": scan_lmdw,
}

CUR_SYM = {"JPY": "¥", "EUR": "€", "GBP": "£", "KRW": "₩", "USD": "$"}
DEFAULT_CUR = {"rudder": "JPY", "mukawa": "JPY", "shinanoya": "JPY", "deinwhisky": "EUR", "vitalaus": "KRW"}


def fmt_price(it):
    p = it.get("price")
    if not p:
        return "가격 미확인"
    return f"{CUR_SYM.get(it.get('cur'), '')}{p}"


_TERMINAL_NOTIFIER_CANDIDATES = [
    "/opt/homebrew/bin/terminal-notifier",  # Apple Silicon brew
    "/usr/local/bin/terminal-notifier",     # Intel brew
]


def notify(title, msg, url=None):
    """macOS 네이티브 알림 (Slack 없이). 실패해도 조용히 넘어감 — 알림은 부가 기능, 인박스가 진짜 기록.
    terminal-notifier가 있으면 그걸 써서 클릭 시 url을 바로 연다(brew install terminal-notifier).
    ⚠️ launchd는 기본 PATH가 /usr/bin:/bin:/usr/sbin:/sbin뿐이라 shutil.which로는 brew 설치
    바이너리를 못 찾는다(2026-09-18 실측 확인) — 절대경로를 직접 확인한다.
    terminal-notifier가 없으면 osascript로 폴백(클릭해도 안 열림, 텍스트만 표시)."""
    import subprocess
    tn_path = next((p for p in _TERMINAL_NOTIFIER_CANDIDATES if Path(p).exists()), None)
    try:
        if url and tn_path:
            subprocess.run(
                [tn_path, "-title", title, "-message", msg,
                 "-open", url, "-sound", "Glass"],
                timeout=5, capture_output=True)
        else:
            esc = lambda s: s.replace("\\", "\\\\").replace('"', '\\"')
            script = f'display notification "{esc(msg)}" with title "{esc(title)}" sound name "Glass"'
            subprocess.run(["osascript", "-e", script], timeout=5, capture_output=True)
    except Exception:
        pass


def send_email(subject, body_text, to_addr):
    """Gmail SMTP(앱 비밀번호)로 자기 자신에게 발송. macOS 알림의 보조 채널 — 실패해도 조용히 넘어감.
    비밀번호는 GMAIL_APP_PASSWORD_FILE(~/.config, 시크릿 표준 위치)에서 읽는다.
    파일이 없으면(=아직 설정 안 함) 조용히 스킵 — 매번 에러 로그로 스팸하지 않는다."""
    raw = os.environ.get("GMAIL_APP_PASSWORD")  # 클라우드: GitHub Secret
    if raw is None:
        if not GMAIL_APP_PASSWORD_FILE.exists():
            return
        raw = GMAIL_APP_PASSWORD_FILE.read_text()
    try:
        # 앱 비밀번호는 "xxxx xxxx xxxx xxxx" 형태로 표시되는데, 웹페이지에서 복사하면
        # 그룹 사이에 일반 스페이스가 아니라 줄바꿈방지공백(U+00A0) 등이 섞여 들어오는 경우가
        # 있다(2026-09-18 실측) — 종류 상관없이 모든 공백을 제거해 순수 16자로 만든다.
        app_password = "".join(raw.split())
        if not app_password:
            return

        import smtplib
        from email.mime.text import MIMEText

        msg = MIMEText(body_text, "plain", "utf-8")
        msg["Subject"] = subject
        msg["From"] = to_addr
        msg["To"] = to_addr

        with smtplib.SMTP("smtp.gmail.com", 587, timeout=10) as server:
            server.starttls()
            server.login(to_addr, app_password)
            server.sendmail(to_addr, [to_addr], msg.as_string())
    except Exception as e:
        log(f"  [ERR] send_email: {e}")


def wait_for_network(timeout=25):
    """DNS가 풀릴 때까지 최대 timeout초 대기. 풀리면 True, 끝내 안 풀리면 False."""
    import socket
    deadline = time.time() + timeout
    while True:
        try:
            socket.getaddrinfo("mukawa-spirit.com", 443)
            return True
        except OSError:
            if time.time() >= deadline:
                return False
            time.sleep(3)


def main():
    args = sys.argv[1:]
    wl = load_json(WATCHLIST, {})
    seen = load_json(SEEN, {"items": {}, "last_ok": None})

    if "--test-email" in args:
        addr = wl.get("notify", {}).get("email")
        send_email("[테스트] 위스키 워치 (클라우드)",
                   "GitHub Actions에서 보낸 테스트 메일입니다. 이 메일이 왔으면 클라우드 발송이 정상입니다.", addr)
        log(f"[TEST] 테스트 메일 발송 시도 → {addr}")
        return

    if "--status" in args:
        by_site = {}
        for k in seen["items"]:
            s = k.split(":", 1)[0]
            by_site[s] = by_site.get(s, 0) + 1
        n_inbox = INBOX.read_text().count("\n- ") if INBOX.exists() else 0
        print(f"마지막 폴링 {seen.get('last_ok') or '없음'} / 인박스 {n_inbox}건")
        print(f"보유 상품 수: {by_site}")
        return

    # 스로틀
    if "--force" not in args and seen.get("last_ok"):
        last = datetime.fromisoformat(seen["last_ok"])
        if datetime.now() - last < timedelta(hours=THROTTLE_H):
            return  # 조용히 스킵 (launchd 중복 기동)

    # 네트워크 대기 — 맥이 DarkWake(2~3초)로만 깨어 있으면 와이파이가 아직 안 붙어 전 사이트가
    # DNS 실패한다(2026-09-18 실측). 안 붙으면 폴링을 건너뛰고 last_ok도 갱신하지 않는다
    # (거짓 성공으로 스로틀이 다음 시도를 막지 않도록).
    if not wait_for_network():
        log("[SKIP] 네트워크 없음(DarkWake/슬립 추정) — 폴링 건너뜀, last_ok 미갱신")
        return

    kws = wl.get("keywords_en", []) + wl.get("keywords_ko", []) + wl.get("keywords_ja", [])
    known = seen["items"]
    new_hits, sale_hits, errors = [], [], 0
    total_fetched = 0
    per_site = {}
    all_items = []

    # --deep: 리포트 직전(08·19시)에 도는 정밀 스캔. 최신 1~2페이지 대신 카탈로그 전체를 읽어
    # 오래된 상품의 재입고(품절→재고)까지 잡는다. 이때 처음 보는 상품은 '신규'가 아니라 그냥
    # 아직 안 본 옛 상품이므로 조용히 기록만 한다(진짜 신규는 평소 실행의 앞 페이지에서 잡힌다).
    deep = "--deep" in args
    events = []

    # 사이트 수집은 병렬로(서로 독립), 판정·상태 갱신은 아래에서 순서대로 한다.
    active = [(site, cfg) for site, cfg in wl.get("sites", {}).items()
              if cfg.get("enabled", True) and SCANNERS.get(cfg.get("type", site))]

    def _scan(pair):
        site, cfg = pair
        try:
            return site, dedup_merge(SCANNERS[cfg.get("type", site)](dict(cfg, _site=site, _deep=deep), kws)), None
        except Exception as e:
            return site, None, e

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=8) as ex:
        scanned = list(ex.map(_scan, active))

    for (site, cfg), (_, items, err) in zip(active, scanned):
        # 처음 붙인 샵은 첫 실행을 '기준선'으로만 기록한다 — 안 그러면 카탈로그 수천 개 중
        # 키워드에 걸리는 기존 상품 수백 건이 한꺼번에 '신규'로 쏟아진다.
        seeded = any(k.startswith(site + ":") for k in known)
        if err is not None:
            log(f"  [ERR] {site}: {err}")
            errors += 1
            continue

        for it in items:                       # 기존 스캐너는 통화를 안 달아서 사이트 기본값으로 채운다
            it.setdefault("cur", cfg.get("cur") or DEFAULT_CUR.get(site))
        total_fetched += len(items)
        per_site[site] = len(items)
        if not seeded and items:
            log(f"  [BASELINE] {site} 첫 수집 {len(items)}개 — 기준선으로만 기록(알림 없음)")
        all_items.extend(items)
        for it in items:
            iid = it["id"]
            kw = matches_keyword(it["title"], kws)
            prev = known.get(iid)
            price = it.get("price")

            avail = it.get("available")
            if prev is None:
                # 신규 상품 등장
                known[iid] = {"title": it["title"], "price": price, "url": it["url"],
                               "on_sale_flag": it.get("on_sale_flag", False), "available": avail,
                               "first_seen": datetime.now().isoformat(timespec="seconds")}
                if seeded and not deep:
                    events.append(_event("new", it, kw))
                    if kw:
                        new_hits.append((it, kw))
            else:
                # 기존 상품 — "세일"은 실제 가격 하락(직전 대비 2%↑)일 때만 인정한다.
                # HTML 배지(is--discount 등)는 같은 상품이라도 카테고리 페이지마다 렌더링이
                # 달라 단독 신호로는 못 믿는다(2026-07-18 DeinWhisky 실측 — Rosebank 오탐 사례).
                # RUDDER는 예외: compare_at_price가 Shopify API의 구조화 필드라 신뢰 가능.
                prev_price = prev.get("price")
                price_drop = (price and prev_price and
                              float(price) < float(prev_price) * 0.98)
                structured_sale = (it["site"] == "rudder" and it.get("on_sale_flag")
                                    and not prev.get("on_sale_flag"))
                if (price_drop or structured_sale) and kw:
                    sale_hits.append((it, kw, prev_price))
                # 재입고: 직전에 품절로 기록됐던 상품이 재고로 바뀜(재고 정보를 주는 샵만 해당)
                if prev.get("available") is False and avail is True:
                    events.append(_event("restock", it, kw))
                known[iid] = {"title": it["title"], "price": price, "url": it["url"],
                               "on_sale_flag": it.get("on_sale_flag", False),
                               "available": avail if avail is not None else prev.get("available"),
                               "first_seen": prev.get("first_seen")}

    # 샵 리포트(shop_digest.py)용 이벤트 적재 — 신규·재입고. 필터(위스키 한정)는 리포트 쪽에서 한다.
    if events:
        with (DATA / "shop_events.jsonl").open("a") as f:
            for e in events:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")
    log(f"  [EVENT] 신규 {sum(e['type'] == 'new' for e in events)} · 재입고 {sum(e['type'] == 'restock' for e in events)}"
        + (" (정밀 스캔)" if deep else ""))

    # 보틀 추적기(bottle_tracker.py)가 후보 탐색에 쓰도록 이번 폴링의 전체 상품을 저장
    try:
        (DATA / "latest_items.json").write_text(json.dumps(all_items, ensure_ascii=False))
    except Exception as e:
        log(f"  [ERR] latest_items 저장: {e}")

    # 정상 폴링이면 항상 상품이 수백 개 나온다 — 0개면 전 사이트 실패(네트워크 등)로 보고
    # 실패 처리(seen.json·last_ok 갱신 안 함). 이전엔 이 경우도 "[OK] 신규 0건"으로 기록했다.
    if total_fetched == 0:
        log("[FAIL] 전 사이트에서 상품 0개 수집 — 실패 처리, last_ok 미갱신")
        return

    # 인박스 기록
    if new_hits or sale_hits:
        DATA.mkdir(parents=True, exist_ok=True)
        fresh = not INBOX.exists() or INBOX.stat().st_size == 0
        with INBOX.open("a") as f:
            if fresh:
                f.write("# 위스키 매물 인박스\n\n브리핑 전 신규/세일 매물. whisky 스킬이 읽고 취향 매칭 후 archive로 옮긴다.\n")
            f.write(f"\n## 수집 {datetime.now().strftime('%Y-%m-%d %H:%M')}\n")
            for it, kw in sorted(new_hits, key=lambda x: x[0]["site"]):
                price_s = fmt_price(it)
                f.write(f"- 🆕 [{it['site']}] **{it['title']}** (매칭: {kw}) — {price_s} — {it['url']}\n")
            for it, kw, prev_price in sorted(sale_hits, key=lambda x: x[0]["site"]):
                f.write(f"- 💰 [{it['site']}] **{it['title']}** (매칭: {kw}) — "
                        f"{CUR_SYM.get(it.get('cur'), '')}{prev_price} → {fmt_price(it)} — {it['url']}\n")

    seen["last_ok"] = datetime.now().isoformat(timespec="seconds")
    seen["items"] = known
    DATA.mkdir(parents=True, exist_ok=True)
    SEEN.write_text(json.dumps(seen, ensure_ascii=False, indent=1))

    log(f"[OK] 폴링 완료 → 신규 {len(new_hits)}건, 세일 {len(sale_hits)}건"
        + (f", 사이트 오류 {errors}건" if errors else "")
        + " | 수집 " + ", ".join(f"{k} {v}" for k, v in per_site.items()))

    if new_hits or sale_hits:
        all_hits = [(it, kw) for it, kw in new_hits] + [(it, kw) for it, kw, _ in sale_hits]
        lines = [f"[{it['site']}] {it['title']}" +
                 (f" ({fmt_price(it)})" if it.get("price") else "")
                 for it, kw in all_hits[:3]]
        if len(all_hits) > 3:
            lines.append(f"...외 {len(all_hits) - 3}건")
        subject = f"위스키 신규 {len(new_hits)}건 · 세일 {len(sale_hits)}건"
        notify(subject, "\n".join(lines), url=all_hits[0][0]["url"])

        try:
            import tg
            chat = os.environ.get("TELEGRAM_CHAT_ID") or load_json(DATA / "tracker_state.json", {}).get("chat_id")
            if tg.TOKEN and chat:
                tg.send(chat, "<b>" + tg.esc(subject) + "</b>\n" + "\n".join(
                    f"· [{it['site']}] {tg.esc(it['title'])} {fmt_price(it) if it.get('price') else ''}\n  {it['url']}" for it, kw in all_hits))
        except Exception as e:
            log(f"  [ERR] telegram: {e}")

        email_addr = wl.get("notify", {}).get("email")
        if email_addr:
            body_lines = [f"[{it['site']}] {it['title']} — "
                          f"{fmt_price(it)}\n  {it['url']}"
                          for it, kw in all_hits]
            send_email(subject, "\n\n".join(body_lines), email_addr)


if __name__ == "__main__":
    main()
