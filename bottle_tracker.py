#!/usr/bin/env python3
"""
bottle_tracker.py — 노션 위시리스트에서 "추적" 체크된 보틀의 입고·가격을 사이트별로 추적한다 (토큰 0)

흐름 (whisky_watch.py 폴링 직후 같은 워크플로에서 실행):
  1. 텔레그램 명령 처리 (/start /add /list /stop /link /help) → 노션에 반영
  2. 노션 DB에서 추적=✓ 행 조회 (상품명·추적 키워드·추적 링크·목표가KRW)
  3. 추적 링크(사이트별 확정 URL)는 상품 페이지를 직접 열어 재고·가격 확인
     - RUDDER   : Shopify {url}.js → available / price(÷100)
     - Mukawa   : 연령확인 POST(restricted_age_agree=1) 후 Colorme JSON → stock_num / sales_price_including_tax, 404=삭제
     - Shinanoya: schema.org ld+json (name/price/availability)
     - DeinWhisky: schema.org/InStock + itemprop=price
  4. 링크가 없는 사이트는 폴링 결과(data/latest_items.json)에서 키워드로 후보를 찾아 텔레그램으로 제안
  5. 변화(재입고·품절·가격하락·목표가 진입)를 텔레그램으로 알리고, 노션 행에 현황을 써준다
  6. data/price_history.csv에 가격 이력 적재

노션 속성: 추적(checkbox) 추적 키워드(text, 콤마 구분 별칭) 추적 링크(text, 줄바꿈 구분 URL)
          목표가KRW(number) → 쓰기: 추적 현황(text) 최근확인가(number) 통화(select) 재고(select) 확인시각(date)

환경변수: NOTION_TOKEN, TELEGRAM_BOT_TOKEN, (선택) NOTION_DATA_SOURCE_ID, TELEGRAM_CHAT_ID, WHISKY_DATA_DIR
로컬 테스트: python3 bottle_tracker.py --dry wanted.json  (노션 대신 파일에서 읽고 쓰기 없음)
"""
import csv, html, http.cookiejar, json, os, re, sys, time, urllib.error, urllib.parse, urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import tg

DATA = Path(os.environ.get("WHISKY_DATA_DIR", Path.home() / "Claude/Projects/위스키/모니터링"))
WATCHLIST = Path(os.environ.get("WHISKY_WATCHLIST", Path(__file__).with_name("watchlist.json")))
STATE = DATA / "tracker_state.json"
HISTORY = DATA / "price_history.csv"
LATEST = DATA / "latest_items.json"
LOG = DATA / "watch.log"

NOTION_TOKEN = os.environ.get("NOTION_TOKEN", "")
DS_ID = os.environ.get("NOTION_DATA_SOURCE_ID", "d26a9b2e-76a2-4d6a-bc0c-31d10ce5fa2f")
DB_ID = os.environ.get("NOTION_DATABASE_ID", "eed23479c4074bc0a710f780570e6369")

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
KST = timezone(timedelta(hours=9))

SITE_BY_HOST = {
    "theultimatespirits.jp": "rudder", "mukawa-spirit.com": "mukawa",
    "deinwhisky.de": "deinwhisky", "shinanoya-tokyo.jp": "shinanoya", "vitalaus.com": "vitalaus",
    "thewhiskybarrel.com": "whiskybarrel", "inn-out-shop.com": "innout", "topwhiskies.com": "topwhiskies",
    "abbeywhisky.com": "abbey", "reallygoodwhisky.com": "reallygood", "whiskyinternationalonline.com": "wio",
    "htfw.com": "htfw", "whisky-maniac.de": "whiskymaniac", "nickollsandperks.com": "nickolls",
    "whiskysite.nl": "whiskysite",
}
SITE_LABEL = {"rudder": "RUDDER", "mukawa": "무카와", "deinwhisky": "DeinWhisky",
              "shinanoya": "시나노야", "vitalaus": "비탈라우스",
              "whiskybarrel": "TWB", "innout": "Inn-Out", "topwhiskies": "Top Whiskies",
              "abbey": "Abbey", "reallygood": "Really Good", "wio": "WIO", "htfw": "HTFW", "whiskymaniac": "Whisky-Maniac", "nickolls": "N&P", "whiskysite": "Whiskysite", "other": "기타"}
SITE_CUR = {"rudder": "JPY", "mukawa": "JPY", "shinanoya": "JPY", "deinwhisky": "EUR", "vitalaus": "KRW",
            "whiskybarrel": "GBP", "innout": "EUR", "topwhiskies": "GBP", "abbey": "GBP",
            "reallygood": "GBP", "wio": "GBP", "htfw": "GBP", "whiskymaniac": "EUR", "nickolls": "GBP", "whiskysite": "EUR"}
CUR_SYM = {"JPY": "¥", "EUR": "€", "KRW": "₩", "GBP": "£"}


def now():
    return datetime.now(KST)


def log(msg):
    DATA.mkdir(parents=True, exist_ok=True)
    line = f"{now().strftime('%Y-%m-%d %H:%M:%S')}  [TRK] {msg}"
    with LOG.open("a") as f:
        f.write(line + "\n")
    print(line)


def load_json(p, default):
    try:
        return json.loads(p.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def qurl(u):
    return urllib.parse.quote(u, safe=":/?=&%#+")


def fetch(url, timeout=20, encoding="utf-8", opener=None, data=None, headers=None):
    h = {"User-Agent": UA}
    h.update(headers or {})
    req = urllib.request.Request(qurl(url), headers=h, data=data)
    op = opener or urllib.request.build_opener()
    with op.open(req, timeout=timeout) as r:
        return r.read().decode(encoding, errors="replace")


def site_of(url):
    host = urllib.parse.urlparse(url).netloc.lower()
    for h, s in SITE_BY_HOST.items():
        if host.endswith(h):
            return s
    return "other"


def norm_url(u):
    p = urllib.parse.urlparse(u.strip())
    path = p.path.rstrip("/")
    q = p.query
    # mukawa는 ?pid= 가 식별자라 쿼리를 남기고, 나머지는 쿼리(?c=45 등) 제거
    if "mukawa" in p.netloc:
        m = re.search(r"pid=(\d+)", q)
        return f"https://mukawa-spirit.com/?pid={m.group(1)}" if m else u.strip()
    return f"{p.scheme}://{p.netloc}{path}"


# ---------------------------------------------------------------- 환율
def fx_rates(wl):
    """KRW 기준 환율 {JPY: 원/엔, EUR: 원/유로}. 하루 1회 open.er-api.com, 실패 시 캐시→watchlist fx."""
    cache = DATA / "fx.json"
    c = load_json(cache, {})
    need = {"JPY", "EUR", "GBP", "KRW"}                  # 통화가 하나라도 빠졌으면 캐시를 버린다
    if c.get("date") == now().strftime("%Y-%m-%d") and need <= set(c.get("rates", {})):
        return c["rates"]
    try:
        d = json.loads(fetch("https://open.er-api.com/v6/latest/KRW", timeout=10))
        r = d["rates"]
        rates = {"JPY": round(1 / r["JPY"], 4), "EUR": round(1 / r["EUR"], 2),
                 "GBP": round(1 / r["GBP"], 2), "KRW": 1.0}
        cache.write_text(json.dumps({"date": now().strftime("%Y-%m-%d"), "rates": rates}))
        return rates
    except Exception as e:
        log(f"환율 조회 실패({e}) — 캐시/기본값 사용")
        return c.get("rates") or {**{"JPY": 8.8, "EUR": 1590, "GBP": 1800, "KRW": 1.0}, **wl.get("fx", {})}


def to_krw(price, cur, rates):
    if price is None:
        return None
    return int(round(float(price) * rates.get(cur, 1.0)))


def fmt_price(price, cur):
    if price is None:
        return "가격 미확인"
    return f"{CUR_SYM.get(cur, cur + ' ')}{int(round(float(price))):,}"


# ---------------------------------------------------------------- 노션
def notion(method, path, body=None, version="2025-09-03"):
    req = urllib.request.Request(
        "https://api.notion.com/v1" + path,
        data=json.dumps(body).encode() if body is not None else None, method=method,
        headers={"Authorization": f"Bearer {NOTION_TOKEN}", "Notion-Version": version,
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        body_txt = e.read().decode(errors="replace")[:300]
        raise RuntimeError(f"notion {method} {path} → {e.code}: {body_txt}")


def notion_query_tracked():
    flt = {"filter": {"property": "추적", "checkbox": {"equals": True}}, "page_size": 100}
    results = []
    for path, ver in ((f"/data_sources/{DS_ID}/query", "2025-09-03"),
                      (f"/databases/{DB_ID}/query", "2022-06-28")):
        try:
            cursor = None
            while True:
                b = dict(flt)
                if cursor:
                    b["start_cursor"] = cursor
                r = notion("POST", path, b, ver)
                results += r.get("results", [])
                cursor = r.get("next_cursor")
                if not r.get("has_more"):
                    break
            return results
        except RuntimeError as e:
            log(f"노션 조회 실패({path}): {e} — 폴백 시도")
            results = []
    raise RuntimeError("노션 조회 실패 (data_source·database 둘 다)")


def _rt(prop):
    t = prop.get("type")
    if t == "title":
        return "".join(x.get("plain_text", "") for x in prop.get("title", []))
    if t == "rich_text":
        return "".join(x.get("plain_text", "") for x in prop.get("rich_text", []))
    return ""


def parse_page(pg):
    p = pg["properties"]
    g = lambda k: p.get(k, {})
    return {
        "id": pg["id"],
        "name": _rt(g("상품명")),
        "aliases": _rt(g("추적 키워드")),
        "links": _rt(g("추적 링크")),
        "target_krw": (g("목표가KRW").get("number")),
        "status_text": _rt(g("추적 현황")),
        "stock": ((g("재고").get("select") or {}).get("name")),
        "wb_id": _rt(g("WB_ID")),
    }


def notion_find_existing(wb_id=None, url=None, title=None):
    """중복 방지 — WB_ID, 추적 링크에 같은 URL, 또는 같은 상품명이 있는 행을 찾는다. 없으면 None."""
    if not NOTION_TOKEN:
        return None
    ors = []
    if wb_id:
        ors.append({"property": "WB_ID", "rich_text": {"equals": str(wb_id)}})
    if url:
        ors.append({"property": "추적 링크", "rich_text": {"contains": norm_url(url)}})
    if title:
        ors.append({"property": "상품명", "title": {"equals": title}})
    if not ors:
        return None
    flt = {"filter": {"or": ors} if len(ors) > 1 else ors[0], "page_size": 5}
    for path, ver in ((f"/data_sources/{DS_ID}/query", "2025-09-03"), (f"/databases/{DB_ID}/query", "2022-06-28")):
        try:
            r = notion("POST", path, flt, ver)
            res = [parse_page(pg) for pg in r.get("results", [])]
            # 중복 표시된 행은 제외
            res = [b for b in res if not b["name"].startswith("[중복")]
            return res[0] if res else None
        except RuntimeError as e:
            log(f"기존 행 조회 실패({path}): {e}")
    return None


def notion_update(page_id, props):
    notion("PATCH", f"/pages/{page_id}", {"properties": props}, "2022-06-28")


def notion_create_tracked(name, aliases, extra=None):
    props = {
        "상품명": {"title": [{"text": {"content": name[:200]}}]},
        "상태": {"select": {"name": "관심"}},
        "추적": {"checkbox": True},
        "추적 키워드": {"rich_text": [{"text": {"content": aliases[:1900]}}]},
    }
    props.update(extra or {})
    for parent, ver in (({"data_source_id": DS_ID}, "2025-09-03"), ({"database_id": DB_ID}, "2022-06-28")):
        try:
            return notion("POST", "/pages", {"parent": parent, "properties": props}, ver)
        except RuntimeError as e:
            log(f"노션 생성 실패({parent}): {e} — 폴백")
    raise RuntimeError("노션 페이지 생성 실패")


# ---------------------------------------------------------------- 상품 페이지 확인
_mukawa_opener = None


def check_rudder(url):
    js = json.loads(fetch(url.split("?")[0] + ".js"))
    price = js.get("price")
    return {"in_stock": bool(js.get("available")), "price": (price / 100) if price is not None else None,
            "title": js.get("title", "")}


def check_mukawa(url):
    global _mukawa_opener
    if _mukawa_opener is None:
        cj = http.cookiejar.CookieJar()
        _mukawa_opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    try:
        page = fetch(url, encoding="euc-jp", opener=_mukawa_opener)
        if '"page":"age_restriction"' in page:
            page = fetch(url, encoding="euc-jp", opener=_mukawa_opener,
                         data=b"restricted_age_agree=1",
                         headers={"Referer": url, "Content-Type": "application/x-www-form-urlencoded"})
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return {"in_stock": False, "price": None, "title": "", "note": "페이지 삭제(404)"}
        raise
    m = re.search(r"var Colorme = (\{.*?\});", page, re.S)
    if not m:
        raise RuntimeError("Colorme JSON 없음")
    pr = json.loads(m.group(1)).get("product", {})
    stock = pr.get("stock_num")
    price = pr.get("sales_price_including_tax") or pr.get("sales_price")
    return {"in_stock": (stock or 0) > 0, "price": price, "title": pr.get("name", ""), "stock_num": stock}


def check_shinanoya(url):
    """makeshop — <script type="application/ld+json"> Product 블록에 name/price/availability가 있다."""
    page = fetch(url)
    m = re.search(r'<script type="application/ld\+json">\s*(\{.*?\})\s*</script>', page, re.S)
    name, price, avail = "", None, None
    if m:
        try:
            d = json.loads(m.group(1))
            name = d.get("name", "")
            off = d.get("offers") or {}
            price = float(str(off.get("price", "")).replace(",", "")) if off.get("price") else None
            avail = off.get("availability", "")
        except json.JSONDecodeError:
            pass
    if avail:
        in_stock = "InStock" in avail
    else:
        sm = re.search(r"在庫数[：:]\s*(\d+)", page)
        in_stock = (int(sm.group(1)) > 0) if sm else ("makeshop-item-cart-entry-url" in page)
    sm = re.search(r"在庫数[：:]\s*(\d+)", page)
    return {"in_stock": in_stock, "price": price, "title": html.unescape(name),
            "stock_num": int(sm.group(1)) if sm else None}


def check_deinwhisky(url):
    page = fetch(url)
    in_stock = "schema.org/InStock" in page
    pm = re.search(r'itemprop="price"\s+content="([\d.]+)"', page)
    if not pm:
        pm2 = re.search(r'class="price--default[^"]*">\s*([\d.,]+)\s*&nbsp;&euro;', page)
        price = float(pm2.group(1).replace(".", "").replace(",", ".")) if pm2 else None
    else:
        price = float(pm.group(1))
    tm = re.search(r'class="product--title"[^>]*>\s*([^<]+?)\s*<', page) or re.search(r"<title>([^<]*)</title>", page)
    return {"in_stock": in_stock, "price": price, "title": html.unescape(tm.group(1)) if tm else ""}


CHECKERS = {"rudder": check_rudder, "mukawa": check_mukawa, "shinanoya": check_shinanoya, "deinwhisky": check_deinwhisky}
# Shopify 샵은 RUDDER와 같은 방식({url}.js — 샵 기준 통화, 접속 국가 영향 없음)으로 확인한다
for _s in ("whiskybarrel", "innout", "topwhiskies", "abbey", "reallygood", "wio"):
    CHECKERS[_s] = check_rudder


# ---------------------------------------------------------------- 키워드 후보
_AGE_RE = re.compile(r"(?<!\d)(\d{1,2})\s*(?:年|years?\s*old|years?|yo|jahre|y\.o\.)(?!\d)", re.I)


def title_age(title):
    """제목에 명시된 숙성연수(예: '34年', '23 Year Old', '12 Jahre'). 없으면 None."""
    m = _AGE_RE.search(title or "")
    return int(m.group(1)) if m else None


_NOT_AGE_PREV = {"batch", "배치", "バッチ", "cask", "캐스크", "カスク", "chapter", "ch", "ch.", "no", "no.", "#",
                 "release", "릴리즈", "リリース", "edition", "에디션", "vol", "vol.", "lot", "bottle", "bottling"}


def query_ages(text):
    """검색어/별칭에서 숙성연수로 볼 숫자(3~70)만 뽑는다 — 배치·캐스크·챕터·No. 뒤의 숫자는 제외."""
    toks = [t for t in re.split(r"\s+", (text or "").lower().replace("#", " # ")) if t]
    ages = []
    for i, t in enumerate(toks):
        t2 = re.sub(r"(년|yo|years?|jahre)$", "", t)
        if t2.isdigit() and 3 <= int(t2) <= 70 and (i == 0 or toks[i - 1] not in _NOT_AGE_PREV):
            ages.append(int(t2))
    return ages


def age_conflict(title, ages):
    """검색어에 숙성연수(query_ages)가 있고 제목에도 명시 연수가 있는데 서로 다르면 True.
    'Chapter 23'이 23년으로 오인되거나(2026-09-22 실측), 34年 병이 23년 검색에 걸리는 것을 막는다."""
    ta = title_age(title)
    return bool(ages) and ta is not None and ta not in ages


# 상품명에서 검색어를 만들 때 버리는 범용 단어 — 사이트마다 표기가 달라 매칭만 방해한다
_STOP = {"year", "years", "old", "yo", "single", "malt", "whisky", "whiskey", "scotch", "cask", "casks",
         "edition", "release", "bottle", "bottling", "the", "of", "&", "and", "strength", "limited",
         "original", "new", "design", "batch", "no", "no.", "vol", "vol."}


def alias_list(bottle):
    raw = bottle["aliases"].strip()
    if raw:
        return [a.strip() for a in re.split(r"[,\n]", raw) if a.strip()]
    # 별칭이 없으면 상품명에서 용량·도수·괄호·범용 단어를 빼고 "증류소 + 숫자/특징어"만 남긴다
    n = re.sub(r"\([^)]*\)", " ", bottle["name"])
    n = re.sub(r"\b\d+\s*ml\b|\b\d+(\.\d+)?\s*%|\d+°|\b\d+\s*proof\b", " ", n, flags=re.I)
    toks = [t for t in re.split(r"\s+", n.strip()) if t and t.lower().strip("(),") not in _STOP]
    return [" ".join(toks)] if toks else []


def title_matches(title, alias):
    """별칭의 모든 토큰이 제목에 있어야 매칭. 매칭되면 토큰 수(구체성)를 돌려준다."""
    t = title.lower()
    toks = [x for x in re.split(r"\s+", alias.lower()) if x]
    return len(toks) if toks and all(x in t for x in toks) else 0


def find_candidates(bottle, items, exclude_urls):
    """구체적인 별칭(토큰 많은)에 걸린 순으로 정렬. 가타카나 한 단어 같은 넓은 별칭은 뒤로 밀린다.
    exclude_urls: 이 보틀 및 다른 보틀에 이미 확정된 링크(다른 병의 매물을 후보로 내지 않음)."""
    scored = []
    aliases = alias_list(bottle)
    ages = [a for al in aliases for a in query_ages(al)]
    for it in items:
        if norm_url(it["url"]) in exclude_urls:
            continue
        title = it.get("title", "")
        if age_conflict(title, ages):
            continue
        best = max((title_matches(title, a) for a in aliases), default=0)
        if best:
            scored.append((best, it))
    scored.sort(key=lambda x: -x[0])
    return [it for _, it in scored]


# ---------------------------------------------------------------- /add 후보 검색 (한/영/일 혼용)
BRANDS_FILE = Path(__file__).with_name("brands.json")


def _load_brands():
    d = load_json(BRANDS_FILE, {})
    groups = [[x.lower() for x in g] for g in d.get("brands", [])]
    generic = {k.lower(): [x.lower() for x in v] for k, v in d.get("generic", {}).items() if not k.startswith("_")}
    return groups, generic


WB_URL_RE = re.compile(r"whiskybase\.com/whiskies/whisky/(\d+)(?:/([a-z0-9\-]+))?", re.I)


def parse_wb_url(text):
    """위스키베이스 링크 → (wb_id, 검색어, 나머지 텍스트). 페이지는 Cloudflare로 못 읽으니 URL 슬러그만 쓴다.
    예: .../whisky/58360/laphroaig-1991 → ('58360', 'laphroaig 1991', '')"""
    m = WB_URL_RE.search(text)
    if not m:
        return None, text, ""
    wb_id, slug = m.group(1), (m.group(2) or "")
    rest = re.sub(r"https?://\S+", " ", text).strip()
    query = (slug.replace("-", " ") + " " + rest).strip()
    return wb_id, query, rest


def expand_query(q):
    """입력을 토큰으로 쪼개고, 브랜드 표기는 모든 언어 변형으로 펼친다.
    반환: [(variants:list, weight:float)] — variants 중 하나라도 제목에 있으면 그 토큰은 매칭."""
    groups, generic = _load_brands()
    ql = q.lower()
    # 한글 '10년' → '10', 'yo' 등 정리
    ql = re.sub(r"(\d+)\s*(년|yo|years?|jahre)", r"\1", ql)
    ql = ql.replace("년", " ").replace("/", " ")
    toks, used = [], set()
    # 브랜드(여러 단어 표기 포함)를 먼저 통째로 찾는다
    for g in groups:
        for v in sorted(g, key=len, reverse=True):
            if v in ql and v not in used:
                toks.append((g, 2.0))
                used.add(v)
                ql = ql.replace(v, " ")
                break
    for t in re.split(r"[\s,]+", ql):
        t = t.strip("()[]【】")
        if not t:
            continue
        if t in generic:
            vs = generic[t]
            if vs:
                toks.append(([t] + vs, 1.0))
            continue
        if re.fullmatch(r"\d+(\.\d+)?", t):
            toks.append(([t], 1.5))
        elif len(t) >= 2:
            toks.append(([t], 1.0))
    return toks


def search_candidates(q, items, limit=6):
    """현재 매물 전체에서 유사 후보. 브랜드 토큰이 있으면 브랜드가 맞는 것만."""
    toks = expand_query(q)
    ages = query_ages(q)
    if not toks:
        return []
    brand_toks = [t for t in toks if t[1] == 2.0]
    num_toks = [t for t in toks if t[1] == 1.5]
    total = sum(w for _, w in toks)

    def hit(vs, title):
        # 숫자는 통단어로만(15가 2015에 걸리지 않게), 문자열은 부분일치
        # 숫자는 통단어로만(15가 2015에 걸리지 않게, 4는 #004에도 걸리게), 문자열은 부분일치
        return any((re.search(rf"(?<!\d)0*{re.escape(v.lstrip('0') or '0')}(?!\d)", title) if re.fullmatch(r"\d+(\.\d+)?", v) else v in title)
                   for v in vs)

    scored, seen = [], set()
    for it in items:
        title = (it.get("title") or "").lower()
        if brand_toks and not all(hit(vs, title) for vs, _ in brand_toks):
            continue
        if num_toks and not all(hit(vs, title) for vs, _ in num_toks):
            continue  # 숙성연수·빈티지·배치 번호를 적었으면 그 숫자들이 전부 있어야 함(1991 23 → 1989 23 제외)
        if age_conflict(title, ages):
            continue  # 제목의 명시 연수가 검색의 숙성연수와 다르면 다른 병
        if not any(hit(vs, title) for vs, w in toks if w != 1.5):
            continue  # 숫자만 맞는 건 후보가 아님
        score = sum(w for vs, w in toks if hit(vs, title))
        if score <= 0 or (score / total) < 0.5:
            continue
        u = norm_url(it["url"])
        if u in seen:
            continue
        seen.add(u)
        scored.append((score / total, -len(title), it))
    scored.sort(key=lambda x: (-x[0], x[1]))
    return [it for _, _, it in scored[:limit]]


# ---------------------------------------------------------------- 텔레그램 명령
HELP = ("<b>위스키 추적 봇</b>\n"
        "/add 보틀명 — 한/영/일 대충 써도 됨. 현재 매물에서 후보를 찾아 버튼으로 보여주고, 고른 것만 노션에 추가\n"
        "위스키베이스 링크만 보내도 됨(뒤에 '34년 whiskyland'처럼 덧붙이면 후보가 좁혀짐) — WB_ID도 함께 기록\n"
        "/list — 추적 중인 보틀과 현황\n"
        "/link 보틀명일부 URL — 확정 상품 링크 추가\n"
        "/stop 보틀명일부 — 추적 해제\n"
        "/help — 이 도움말")


def handle_telegram(state, dry):
    if not tg.TOKEN:
        return
    offset = state.get("tg_offset")
    updates = tg.get_updates(offset)
    for u in updates:
        state["tg_offset"] = u["update_id"] + 1
        if "callback_query" in u:
            log(f"텔레그램 콜백 수신: {u['callback_query'].get('data')}")
            state.setdefault("_callbacks", []).append(u["callback_query"])
            continue
        log(f"텔레그램 업데이트 수신: {[k for k in u if k != 'update_id']}")
        msg = u.get("message") or u.get("edited_message")
        if not msg or "text" not in msg:
            continue
        chat_id = msg["chat"]["id"]
        text = msg["text"].strip()
        if state.get("chat_id") is None:
            state["chat_id"] = chat_id
            log(f"텔레그램 chat_id 등록: {chat_id}")
        if chat_id != state.get("chat_id"):
            continue  # 등록된 사람만
        cmd, _, arg = text.partition(" ")
        cmd = cmd.lower().split("@")[0]
        # 후보 메시지에 버튼 대신 숫자/취소를 문자로 답한 경우 → 가장 최근 보류 건의 선택으로 간주
        pend = state.get("pending_adds", {})
        mine = sorted((k for k, v in pend.items() if v.get("chat") == chat_id), key=lambda k: pend[k]["ts"])
        if mine and re.fullmatch(r"\d{1,2}|취소|x", text.strip().lower()):
            key = mine[-1]
            choice = "x" if text.strip().lower() in ("취소", "x") else text.strip()
            state.setdefault("_callbacks", []).append(
                {"id": None, "data": f"add:{key}:{choice}",
                 "message": {"chat": {"id": chat_id}, "message_id": pend[key].get("message_id")}})
            continue
        try:
            if cmd in ("/start", "/help"):
                tg.send(chat_id, HELP)
            elif cmd == "/add" or WB_URL_RE.search(text):
                raw = text if cmd != "/add" else arg
                wb_id, name, _ = parse_wb_url(raw.replace("|", " "))
                name = name.strip()
                if not name:
                    tg.send(chat_id, "사용법: /add 보틀명 (예: /add 라프로익 10 cs 배치 17) 또는 위스키베이스 링크")
                else:
                    state.setdefault("_add_queries", []).append((name, chat_id, wb_id))
            elif cmd == "/list":
                state["_want_list"] = chat_id
            elif cmd in ("/stop", "/link"):
                state.setdefault("_pending", []).append((cmd, arg.strip(), chat_id))
            else:
                tg.send(chat_id, "모르는 명령입니다. /help")
        except Exception as e:
            log(f"텔레그램 명령 처리 실패 {cmd}: {e}")
            tg.send(chat_id, f"⚠️ 실패: {tg.esc(e)}")


def offer_add_candidates(state, items, rates, tracked_urls=None, dry=False):
    """/add 질의마다 후보를 찾아 버튼 메시지로 보낸다. 실제 생성은 사용자가 버튼을 누른 뒤(다음 실행)."""
    chat_default = state.get("chat_id")
    pend = state.setdefault("pending_adds", {})
    for entry in state.pop("_add_queries", []):
        q, chat_id, wb_id = (list(entry) + [None])[:3]
        cands = search_candidates(q, items)
        key = f"{int(time.time()) % 100000000:x}"
        pend[key] = {"query": q, "chat": chat_id, "ts": now().isoformat(), "wb_id": wb_id,
                     "cands": [{"site": c["site"], "title": c["title"], "url": c["url"], "price": c.get("price")} for c in cands]}
        existing = None
        if not dry:
            try:
                existing = notion_find_existing(wb_id=wb_id)
            except Exception as e:
                log(f"기존 행 조회 예외: {e}")
        pend[key]["existing"] = {"id": existing["id"], "name": existing["name"]} if existing else None
        head = f"🔍 <b>{tg.esc(q)}</b>" + (f" (WB {wb_id})" if wb_id else "")
        lines = [f"{head} — 현재 매물 중 이 글자가 들어간 것 {len(cands)}건 (같은 병이라는 뜻은 아님, 숙성연수·병입자 확인 후 고르세요)"]
        if existing:
            lines.append(f"📒 노션에 이미 있는 행: <b>{tg.esc(existing['name'])}</b> → <b>0번</b>을 누르면 이 행에 추적을 켭니다(새 행 안 만듦)")
        if wb_id:
            lines.append("위스키베이스 페이지는 봇이 못 읽어 링크의 증류소·빈티지만 씁니다. 링크 뒤에 '23년 OB'처럼 덧붙이면 후보가 좁혀집니다.")
        rows = []
        for i, c in enumerate(cands, 1):
            cur = SITE_CUR.get(c["site"], "KRW")
            k = to_krw(c.get("price"), cur, rates)
            owner = (tracked_urls or {}).get(norm_url(c["url"]))
            lines.append(f"<b>{i}.</b> [{SITE_LABEL.get(c['site'], c['site'])}] {tg.esc(c['title'])} {fmt_price(c.get('price'), cur)}"
                         + (f" (≈{k:,}원)" if k else "")
                         + (f"\n    ⚠️ 이미 추적 중인 매물: {tg.esc(owner)}" if owner else "") + f"\n    {c['url']}")
        if not cands:
            lines.append("현재 5개 사이트 매물에는 비슷한 게 없습니다. 이름만으로 추가해두면 이후 새로 뜰 때 후보를 보내드립니다.")
        btns = [{"text": str(i), "callback_data": f"add:{key}:{i}"} for i in range(1, len(cands) + 1)]
        for i in range(0, len(btns), 4):
            rows.append(btns[i:i + 4])
        rows.append([{"text": ("0: 기존 행에 추적 켜기" if existing else "0: 이름만 추가(후보 없음)"), "callback_data": f"add:{key}:0"},
                     {"text": "취소", "callback_data": f"add:{key}:x"}])
        r = tg.send(chat_id or chat_default, "\n".join(lines), reply_markup={"inline_keyboard": rows})
        try:
            pend[key]["message_id"] = r["result"]["message_id"]
        except (TypeError, KeyError):
            pass
    # 3일 지난 보류 건 정리
    for k in list(pend):
        try:
            if now() - datetime.fromisoformat(pend[k]["ts"]) > timedelta(days=3):
                del pend[k]
        except Exception:
            del pend[k]


def apply_callbacks(state, dry):
    """버튼 응답(add:<key>:<n>) → 노션 행 생성. 메시지를 결과로 편집해 중복 탭을 막는다."""
    pend = state.setdefault("pending_adds", {})
    for cb in state.pop("_callbacks", []):
        data = cb.get("data", "")
        chat_id = cb.get("message", {}).get("chat", {}).get("id")
        msg_id = cb.get("message", {}).get("message_id")
        if cb.get("id"):
            tg.answer_callback(cb["id"])
        m = re.fullmatch(r"add:([0-9a-f]+):(\d+|x)", data)
        if not m:
            continue
        key, choice = m.group(1), m.group(2)
        p = pend.get(key)
        if not p:
            log(f"콜백 키 미존재: {key} (보류 목록: {list(pend)})")
            tg.edit(chat_id, msg_id, "⌛ 만료된 요청입니다. /add 를 다시 보내주세요.")
            continue
        if choice == "x":
            del pend[key]
            tg.edit(chat_id, msg_id, f"🚫 취소: {tg.esc(p['query'])}")
            continue
        n = int(choice)
        if n > len(p["cands"]):
            tg.send(chat_id, f"후보는 1~{len(p['cands'])}번입니다. 다시 답해주세요.")
            continue
        try:
            if n == 0:
                name, aliases, url = p["query"], p["query"], ""
            else:
                c = p["cands"][n - 1]
                name, aliases, url = c["title"], f"{p['query']}, {c['title']}", c["url"]
            target = p.get("existing")  # WB_ID로 이미 찾아둔 행
            if not dry and not target:
                ex = notion_find_existing(url=url or None, title=name if n else None)
                target = {"id": ex["id"], "name": ex["name"], "links": ex["links"], "aliases": ex["aliases"]} if ex else None
            if dry:
                log(f"(dry) 노션 {'갱신' if target else '생성'}: {name} / {url}")
            elif target:
                props = {"추적": {"checkbox": True}}
                old_links = target.get("links") or ""
                if url and norm_url(url) not in old_links:
                    props["추적 링크"] = {"rich_text": [{"text": {"content": (old_links + "\n" + url).strip()}}]}
                old_al = target.get("aliases") or ""
                if p["query"].lower() not in old_al.lower():
                    props["추적 키워드"] = {"rich_text": [{"text": {"content": (old_al + ", " + p["query"]).strip(", ")[:1900]}}]}
                if p.get("wb_id"):
                    props["WB_ID"] = {"rich_text": [{"text": {"content": p["wb_id"]}}]}
                notion_update(target["id"], props)
                name = target["name"]
                log(f"노션 기존 행에 추적 설정(텔레그램): {name}")
            else:
                props_extra = {"추적 링크": {"rich_text": [{"text": {"content": url}}]}} if url else {}
                if p.get("wb_id"):
                    props_extra["WB_ID"] = {"rich_text": [{"text": {"content": p["wb_id"]}}]}
                notion_create_tracked(name, aliases, props_extra)
                log(f"노션 행 생성(텔레그램): {name}")
            del pend[key]
            tg.edit(chat_id, msg_id, f"✅ {'기존 행에 추적 설정' if target else '추가·추적 시작'}: <b>{tg.esc(name)}</b>" + (f"\n{url}" if url else "")
                    + (f"\nWB_ID {p['wb_id']} 기록" if p.get("wb_id") else "")
                    + "\n다음 실행부터 재고·가격을 확인합니다.")
        except Exception as e:
            log(f"콜백 처리 실패: {e}")
            tg.send(chat_id, f"⚠️ 추가 실패: {tg.esc(e)}")


def apply_pending(state, bottles, dry):
    """/stop, /link 는 추적 목록을 읽은 뒤에 처리한다."""
    chat_id = state.get("chat_id")
    for cmd, arg, _ in state.pop("_pending", []):
        if cmd == "/stop":
            key = arg.lower()
        else:
            key, _, url = arg.rpartition(" ")
            key = key.lower().strip()
        hits = [b for b in bottles if key and key in b["name"].lower()]
        if len(hits) != 1:
            names = "\n".join(f"· {tg.esc(b['name'])}" for b in hits) or "(없음)"
            tg.send(chat_id, f"'{tg.esc(key)}'에 해당하는 보틀이 {len(hits)}개라 처리 못 했습니다:\n{names}")
            continue
        b = hits[0]
        if dry:
            tg.send(chat_id, f"(dry) {cmd} {tg.esc(b['name'])}")
            continue
        if cmd == "/stop":
            notion_update(b["id"], {"추적": {"checkbox": False}})
            tg.send(chat_id, f"⏹ 추적 해제: <b>{tg.esc(b['name'])}</b>")
            bottles.remove(b)
        else:
            if not url.startswith("http"):
                tg.send(chat_id, "사용법: /link 보틀명일부 https://...")
                continue
            links = (b["links"] + "\n" + url).strip()
            notion_update(b["id"], {"추적 링크": {"rich_text": [{"text": {"content": links}}]}})
            b["links"] = links
            tg.send(chat_id, f"🔗 링크 추가: <b>{tg.esc(b['name'])}</b>\n{tg.esc(url)}")


# ---------------------------------------------------------------- 메인
def main():
    args = sys.argv[1:]
    dry = "--dry" in args
    wl = load_json(WATCHLIST, {})
    state = load_json(STATE, {"chat_id": None, "tg_offset": None, "bottles": {}})
    if os.environ.get("TELEGRAM_CHAT_ID"):
        state["chat_id"] = int(os.environ["TELEGRAM_CHAT_ID"])
    rates = fx_rates(wl)

    handle_telegram(state, dry)
    apply_callbacks(state, dry)  # 버튼으로 확정된 /add → 노션 행 생성 (조회 전에 해야 이번 실행에 포함)

    # 추적 목록
    if dry:
        src = args[args.index("--dry") + 1] if len(args) > args.index("--dry") + 1 else "wanted.json"
        bottles = load_json(Path(src), [])
        for i, b in enumerate(bottles):
            b.setdefault("id", f"dry-{i}")
            b.setdefault("aliases", "")
            b.setdefault("links", "")
            b.setdefault("target_krw", None)
            b.setdefault("stock", None)
    else:
        if not NOTION_TOKEN:
            log("NOTION_TOKEN 없음 — 추적 건너뜀")
            STATE.write_text(json.dumps(state, ensure_ascii=False, indent=1))
            return
        bottles = [parse_page(pg) for pg in notion_query_tracked()]
    apply_pending(state, bottles, dry)
    log(f"추적 보틀 {len(bottles)}개")

    # 폴링 결과(후보 탐색용)
    items = load_json(LATEST, [])
    if not items:
        try:
            import whisky_watch as ww
            kws = wl.get("keywords_en", []) + wl.get("keywords_ko", []) + wl.get("keywords_ja", [])
            for site, cfg in wl.get("sites", {}).items():
                if cfg.get("enabled", True) and site in ww.SCANNERS:
                    items += ww.dedup_merge(ww.SCANNERS[site](cfg, kws))
        except Exception as e:
            log(f"폴링 결과 없음·직접 수집 실패: {e}")
    latest_by_url = {norm_url(it["url"]): it for it in items}
    tracked_urls = {norm_url(u): b["name"] for b in bottles
                    for u in re.split(r"[\s,]+", b["links"]) if u.strip().startswith("http")}
    offer_add_candidates(state, items, rates, tracked_urls, dry)

    alerts, list_lines = [], []
    hist_rows = []
    ts = now().strftime("%Y-%m-%d %H:%M")
    bstate_all = state.setdefault("bottles", {})

    all_confirmed = {norm_url(u) for b in bottles for u in re.split(r"[\s,]+", b["links"]) if u.strip().startswith("http")}
    for b in bottles:
        bs = bstate_all.setdefault(b["id"], {"links": {}, "suggested": []})
        urls = [norm_url(u) for u in re.split(r"[\s,]+", b["links"]) if u.strip().startswith("http")]
        results = []
        for u in urls:
            site = site_of(u)
            prev = bs["links"].get(u, {})
            cur = SITE_CUR.get(site, "KRW")
            try:
                if site in CHECKERS:
                    r = CHECKERS[site](u)
                elif u in latest_by_url:
                    it = latest_by_url[u]
                    r = {"in_stock": True, "price": float(it["price"]) if it.get("price") else None, "title": it["title"]}
                else:
                    r = {"in_stock": None, "price": None, "title": "", "note": "지원하지 않는 사이트"}
            except Exception as e:
                log(f"  [ERR] {b['name']} / {site}: {e}")
                r = {"in_stock": prev.get("in_stock"), "price": prev.get("price"), "title": prev.get("title", ""), "note": f"확인 실패({type(e).__name__})"}
            krw = to_krw(r.get("price"), cur, rates)
            r.update({"site": site, "url": u, "currency": cur, "krw": krw, "checked": ts})
            results.append(r)
            hist_rows.append([ts, b["id"], b["name"], site, u, r.get("in_stock"), r.get("price"), cur, krw])

            # 알림 판정
            label = SITE_LABEL.get(site, site)
            name = tg.esc(b["name"])
            link = f'<a href="{u}">{label}</a>'
            was = prev.get("in_stock")
            if r.get("in_stock") and was is False:
                alerts.append(f"🟢 <b>재입고</b> {name} — {link} {fmt_price(r.get('price'), cur)}"
                              + (f" (≈{krw:,}원)" if krw else ""))
            elif r.get("in_stock") and was is None:
                alerts.append(f"👀 <b>추적 시작·재고 있음</b> {name} — {link} {fmt_price(r.get('price'), cur)}"
                              + (f" (≈{krw:,}원)" if krw else ""))
            elif r.get("in_stock") is False and was:
                alerts.append(f"🔴 <b>품절</b> {name} — {link}" + (f" ({r['note']})" if r.get("note") else ""))
            pp = prev.get("price")
            if r.get("price") and pp and float(r["price"]) < float(pp) * 0.98:
                alerts.append(f"📉 <b>가격 하락</b> {name} — {link} {fmt_price(pp, cur)} → {fmt_price(r['price'], cur)}"
                              + (f" (≈{krw:,}원)" if krw else ""))
            tgt = b.get("target_krw")
            if tgt and krw and r.get("in_stock") and krw <= tgt and not (prev.get("krw") and prev["krw"] <= tgt):
                alerts.append(f"🎯 <b>목표가 진입</b> {name} — {link} ≈{krw:,}원 ≤ 목표 {int(tgt):,}원")
            bs["links"][u] = {k: r.get(k) for k in ("in_stock", "price", "currency", "krw", "title", "checked", "note", "stock_num")}

        # 후보 제안 (확정 링크가 없는 사이트만)
        covered = {site_of(u) for u in urls}
        cands = [it for it in find_candidates(b, items, set(urls) | all_confirmed)
                 if it["site"] not in covered]
        new_c = [it for it in cands if norm_url(it["url"]) not in bs["suggested"]]
        if new_c:
            shown = new_c[:6]
            lines = [f"🔎 <b>후보 발견</b> {tg.esc(b['name'])} — 맞으면 노션 '추적 링크'에 URL을 넣거나 /link 로 확정"]
            for it in shown:
                cur = SITE_CUR.get(it["site"], "KRW")
                k = to_krw(it.get("price"), cur, rates)
                lines.append(f"  · [{SITE_LABEL.get(it['site'], it['site'])}] {tg.esc(it['title'])} {fmt_price(it.get('price'), cur)}"
                             + (f" (≈{k:,}원)" if k else "") + f"\n    {it['url']}")
            if len(new_c) > len(shown):
                lines.append(f"  … 외 {len(new_c) - len(shown)}건 — 후보가 너무 많으면 노션 '추적 키워드'에 더 구체적인 별칭을 넣어주세요")
            alerts.append("\n".join(lines))
            bs["suggested"] += [norm_url(it["url"]) for it in shown]  # 보낸 것만 기록 — 나머지는 다음 실행에 이어서

        # 노션 현황 텍스트
        in_stock = [r for r in results if r.get("in_stock")]
        best = min(in_stock, key=lambda r: r["krw"] or 10**12) if any(r.get("krw") for r in in_stock) else None
        parts = []
        for r in results:
            lab = SITE_LABEL.get(r["site"], r["site"])
            st = "입고" if r.get("in_stock") else ("품절" if r.get("in_stock") is False else "미확인")
            extra = f"(재고{r['stock_num']})" if r.get("stock_num") else ""
            parts.append(f"{lab} {fmt_price(r.get('price'), r['currency'])} {st}{extra}")
        status = " · ".join(parts) if parts else "확정 링크 없음"
        if best:
            status += f" · 최저 {fmt_price(best['price'], best['currency'])}(≈{best['krw']:,}원) @{SITE_LABEL.get(best['site'])}"
        if cands:
            status += f" · 후보 {len(cands)}건(텔레그램 참조)"
        status += f" · {ts}"
        stock_sel = "입고" if in_stock else ("품절" if results and any(r.get("in_stock") is False for r in results) else "미확인")
        list_lines.append(f"• <b>{tg.esc(b['name'])}</b>\n  {tg.esc(status)}")

        core_old = re.sub(r" · \d{4}-\d{2}-\d{2} \d{2}:\d{2}$", "", b.get("status_text") or "")
        core_new = re.sub(r" · \d{4}-\d{2}-\d{2} \d{2}:\d{2}$", "", status)
        last_write = bs.get("last_write")
        stale = not last_write or (now() - datetime.fromisoformat(last_write)) > timedelta(hours=6)
        if not dry and (core_old != core_new or b.get("stock") != stock_sel or stale):
            props = {
                "추적 현황": {"rich_text": [{"text": {"content": status[:1900]}}]},
                "재고": {"select": {"name": stock_sel}},
                "확인시각": {"date": {"start": now().isoformat(timespec="minutes")}},
            }
            if best:
                props["최근확인가"] = {"number": float(best["price"])}
                props["통화"] = {"select": {"name": best["currency"]}}
            try:
                notion_update(b["id"], props)
                bs["last_write"] = now().isoformat()
            except Exception as e:
                log(f"  [ERR] 노션 갱신 {b['name']}: {e}")

    # 가격 이력
    if hist_rows:
        DATA.mkdir(parents=True, exist_ok=True)
        new = not HISTORY.exists()
        with HISTORY.open("a", newline="") as f:
            w = csv.writer(f)
            if new:
                w.writerow(["ts", "page_id", "bottle", "site", "url", "in_stock", "price", "currency", "price_krw"])
            w.writerows(hist_rows)

    # 텔레그램 발송
    chat = state.get("chat_id")
    if state.pop("_want_list", None):
        tg.send(chat, "<b>추적 중인 보틀</b>\n" + ("\n".join(list_lines) or "(없음)"))
    if alerts:
        if chat:
            tg.send(chat, "\n\n".join(alerts))
        else:
            log("알림 있으나 텔레그램 chat_id 미등록 — 봇에 /start 를 보내야 함")
        for a in alerts:
            log("알림: " + re.sub(r"<[^>]+>", "", a).split("\n")[0])

    # 더 이상 추적하지 않는 보틀 상태 정리
    keep = {b["id"] for b in bottles}
    for k in list(bstate_all):
        if k not in keep:
            del bstate_all[k]

    if not dry or True:  # dry에서도 tg_offset·chat_id는 저장
        STATE.write_text(json.dumps(state, ensure_ascii=False, indent=1))
    log(f"완료 — 알림 {len(alerts)}건, 이력 {len(hist_rows)}행")


if __name__ == "__main__":
    main()
