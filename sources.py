"""
sources.py — 보틀 하나를 여러 경매·리테일 사이트에서 검색하는 수집기 모음 (토큰 0)

각 소스는 search(term) -> [item] 을 제공한다. item 공통 스키마:
  source  소스 키(yahoo/swa/justwhisky/wio)      id      소스 내 고유 id
  title   상품·로트 제목                          url     상세 URL
  price   현재가(없으면 None)                     cur     통화(JPY/GBP)
  kind    "auction" | "retail"                    live    지금 입찰·구매 가능한가
  end     종료 epoch(모르면 None)                 bids    입찰수(모르면 None)
  note    한 줄 부가정보(낙찰 이력 등)

⚠️ 사이트별 함정
- 야후옥션: 검색결과 0건을 **HTTP 404**로 준다. 여러 단어 AND 검색은 매물이 없으면 실패처럼 보이므로
  넓은 한 단어로 긁고 호출부에서 거른다. 간헐적 500은 재시도. 표시가는 스토어 출품이면 税込.
- SWA: 정렬은 GET으로 안 먹고 POST(ft=sort). perpage=100을 POST하면 한 번에 다 온다(세션 쿠키 필요).
  검색은 전체 경매(과거 포함)라 '낙찰가 이력'이 덤으로 나온다. 라이브 판정은 "Sold for"·"failed to sell"이
  **없는** 로트. GET ?page=N 페이징은 작동.
- Just Whisky: /api/lots/ 공개 JSON. 검색 파라미터는 **name** 하나만 먹고 나머지(auction·status 등)는
  조용히 무시된다(전체 건수가 그대로 나오면 필터가 안 먹은 것). 라이브 판정은 on_bidding.
- WIO: Shopify /search/suggest.json. 영국 리테일이라 표시가에 VAT가 포함/미포함인지 상품별로 다를 수 있어
  알림에는 '세전/세후 확인 필요'를 붙인다.
- Whisky.Auction: /auctions/search?src=... (opensearch.xml에 적힌 실제 검색 URL). 진행 중 경매만 나온다.
- Whisky Hammer·Whisky Auctioneer는 평문 요청을 403/404로 막는다(2026-09-23 실측) — 미지원.
  둘 다 일본 배송은 되므로(DHL £70), 매물이 뜨면 사람이 직접 확인해야 한다.
"""
import gzip, html, http.cookiejar, json, re, time, urllib.error, urllib.parse, urllib.request

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/140.0 Safari/537.36")


def _get(url, data=None, opener=None, accept="text/html,*/*;q=0.8", lang="en-GB,en;q=0.9", tries=3):
    body = urllib.parse.urlencode(data).encode() if data else None
    req = urllib.request.Request(url, data=body, headers={
        "User-Agent": UA, "Accept": accept, "Accept-Language": lang})
    op = opener or urllib.request.build_opener()
    for i in range(tries):
        try:
            with op.open(req, timeout=30) as r:
                raw = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                return raw.decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            if e.code < 500 or i == tries - 1:
                raise
            time.sleep(2 * (i + 1))
        except urllib.error.URLError:
            if i == tries - 1:
                raise
            time.sleep(2 * (i + 1))


# ---------------------------------------------------------------- 야후옥션 (일본)
def yahoo(term, pages=3, n=100):
    out = []
    for page in range(pages):
        qs = urllib.parse.urlencode({"p": term, "n": n, "b": page * n + 1, "s1": "new", "o1": "d"})
        try:
            page_html = _get(f"https://auctions.yahoo.co.jp/search/search?{qs}", lang="ja,en;q=0.8")
        except urllib.error.HTTPError as e:
            if e.code == 404:      # 결과 0건
                break
            raise
        items = _parse_yahoo(page_html)
        out += items
        if len(items) < n:
            break
        time.sleep(1)
    return out


def _parse_yahoo(page):
    out = []
    for blk in page.split('<li class="Product">')[1:]:
        m = re.search(r'data-auction-id="([^"]+)"', blk)
        t = re.search(r'data-auction-title="([^"]*)"', blk)
        if not (m and t):
            continue
        num = lambda pat: int(re.search(pat, blk).group(1)) if re.search(pat, blk) else None
        pm = re.search(r'Product__postage[^>]*>\s*＋?送料([\d,]+)円', blk)
        bids = re.search(r'Product__bid">(\d+)<', blk)
        buynow = num(r'data-auction-buynowprice="(\d+)"')
        store = 'data-auction-isshoppingitem="1"' in blk
        note = []
        if buynow:
            note.append(f"즉결 ¥{buynow:,}")
        note.append("스토어(税込)" if store else "개인출품(소비세 없음)")
        out.append({
            "source": "yahoo", "id": m.group(1), "title": html.unescape(t.group(1)),
            "url": f"https://auctions.yahoo.co.jp/jp/auction/{m.group(1)}",
            "price": num(r'data-auction-price="(\d+)"'), "cur": "JPY",
            "kind": "auction", "live": True, "end": num(r'data-auction-endtime="(\d+)"'),
            "bids": int(bids.group(1)) if bids else 0,
            "postage": int(pm.group(1).replace(",", "")) if pm else 0,
            "buynow": buynow, "note": " · ".join(note),
        })
    return out


# ---------------------------------------------------------------- Scotch Whisky Auctions (영국)
_SOLD = re.compile(r"Sold for £([\d,]+)\s*in\s*([A-Za-z]+\s*\d{4})")


def swa_current_auctions(opener=None):
    """지금 입찰 가능한 경매의 내부 id들. 경매 사이일 때는 빈 집합(= 라이브 로트 없음)."""
    page = _get("https://www.scotchwhiskyauctions.com/auctions/current/", opener=opener)
    return {int(x) for x in re.findall(r"/auctions/(\d+)-auction/", page)}


def swa(term, perpage=100):
    """라이브 로트 + (덤으로) 같은 검색어의 최근 낙찰 이력.

    ⚠️ 검색은 과거 경매까지 다 돌려주고, 지난 로트에도 'failed to meet reserve'처럼 낙찰가가 없는
    것이 섞인다. 그래서 라이브 판정은 두 조건을 **모두** 건다 — ① 현재 진행 경매 id에 속하고
    ② 결과 문구(class="sold"/"unsold")가 없다. 경매 사이 기간이면 자연히 0건이 된다.
    """
    cj = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    url = ("https://www.scotchwhiskyauctions.com/auctions/all/?"
           + urllib.parse.urlencode({"q": term, "search": "a"}))
    _get(url, opener=op)                                            # 세션 쿠키 받기
    page = _get(url, data={"ft": "sort", "sortby": "mostrecent", "sortorder": "DESC",
                           "perpage": str(perpage)}, opener=op)
    current = swa_current_auctions(op)
    live, sold = [], []
    for href, body in re.findall(r'<a href="([^"]+)" class="lot">(.*?)</a>', page, re.S):
        text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", body)).strip()
        title = html.unescape(re.sub(r"\s*Lot no.*$", "", text.split(" Sold for")[0]
                                     .split(" Lot failed")[0]).strip())
        m = _SOLD.search(text)
        if m:
            sold.append((m.group(2), int(m.group(1).replace(",", ""))))
            continue
        auc = re.search(r"/auctions/(\d+)-auction/", href)
        ended = re.search(r'<p class="(sold|unsold)"', body)
        if ended or not auc or int(auc.group(1)) not in current:
            continue
        bid = re.search(r"£([\d,]+)", text)
        live.append({
            "source": "swa", "id": href.strip("/").split("/")[-1], "title": title,
            "url": "https://www.scotchwhiskyauctions.com" + href,
            "price": int(bid.group(1).replace(",", "")) if bid else None, "cur": "GBP",
            "kind": "auction", "live": True, "end": None,
            "bids": None, "postage": 0, "buynow": None,
            "note": "낙찰수수료 15%(+수수료에 VAT) 별도",
        })
    # 같은 검색어의 최근 낙찰가를 붙여준다 (정렬 파라미터가 안 먹으므로 직접 날짜순 정렬)
    sold.sort(key=lambda x: _month_key(x[0]), reverse=True)
    for it in live:
        if sold:
            recent = ", ".join(f"£{p:,}({d})" for d, p in sold[:3])
            it["note"] += f" · 최근 낙찰 {recent}"
    return live


_MONTHS = {m: i for i, m in enumerate(
    "january february march april may june july august september october november december".split(), 1)}


def _month_key(text):
    m = re.match(r"([A-Za-z]+)\s*(\d{4})", text.strip())
    return (int(m.group(2)), _MONTHS.get(m.group(1).lower(), 0)) if m else (0, 0)


# ---------------------------------------------------------------- Just Whisky (영국)
def justwhisky(term, page_size=100, max_pages=2):
    out = []
    for page in range(1, max_pages + 1):
        qs = urllib.parse.urlencode({"name": term, "page_size": page_size, "page": page})
        try:
            d = json.loads(_get(f"https://www.just-whisky.co.uk/api/lots/?{qs}",
                                accept="application/json"))
        except urllib.error.HTTPError as e:
            if e.code in (400, 404):
                break
            raise
        rows = d.get("data", {}).get("results", [])
        for r in rows:
            item = r.get("item") or {}
            title = " ".join(x for x in (item.get("title"), item.get("subtitle")) if x)
            if not r.get("on_bidding"):          # 지난 경매 로트는 건너뛴다
                continue
            bid = (r.get("bid_stats") or {}).get("current_bid")
            out.append({
                "source": "justwhisky", "id": str(r.get("id")), "title": title,
                "url": f"https://www.just-whisky.co.uk/lot/{r.get('slug')}",
                "price": int(float(bid)) if bid else None, "cur": "GBP",
                "kind": "auction", "live": True, "end": None,
                "bids": None, "postage": 0, "buynow": None,
                "note": "낙찰수수료 15%(+수수료에 VAT) 별도",
            })
        if not d.get("data", {}).get("next"):
            break
    return out


# ---------------------------------------------------------------- Shopify 리테일 공용 (WIO·TWB·Inn-Out 등)
def shopify(base, source, cur, limit=10):
    """Shopify 샵 검색. 후보는 suggest.json으로 찾고 **가격·재고는 /products/{handle}.js로 다시 읽는다.**

    ⚠️ suggest.json과 쿠키 기반 가격은 접속 국가에 따라 KRW·USD 등으로 바뀐다(Shopify Markets).
    같은 상품이 한국 IP에선 ₩149,753, 기준 통화로는 £97.96이었다(2026-09-25 실측). GitHub 러너는
    미국 IP라 그대로 쓰면 USD가 섞인다. .js는 샵 기준 통화의 최소단위(센트)라 ÷100.
    """
    base = base.rstrip("/")

    def search(term):
        qs = urllib.parse.urlencode({"q": term, "resources[type]": "product", "resources[limit]": limit})
        d = json.loads(_get(f"{base}/search/suggest.json?{qs}", accept="application/json"))
        out = []
        for p in d.get("resources", {}).get("results", {}).get("products", []):
            handle = (p.get("handle") or p.get("url", "").split("/products/")[-1]).split("?")[0]
            if not handle:
                continue
            try:
                js = json.loads(_get(f"{base}/products/{handle}.js", accept="application/json"))
            except Exception:
                continue
            if not js.get("available"):
                continue
            price = js.get("price")
            out.append({
                "source": source, "id": str(js.get("id") or handle),
                "title": html.unescape(js.get("title", "")),
                "url": f"{base}/products/{handle}",
                "price": round(price / 100, 2) if price is not None else None, "cur": cur,
                "kind": "retail", "live": True, "end": None,
                "bids": None, "postage": 0, "buynow": None,
                "note": "리테일 즉시구매",
            })
        return out
    return search


wio = shopify("https://whiskyinternationalonline.com", "wio", "GBP")


# ---------------------------------------------------------------- Whisky.Auction (영국, 런던)
def whiskyauction(term):
    """현재 진행 경매의 로트만 돌려준다(검색이 애초에 진행 중 경매 대상). 종료까지 남은 초는 data-left."""
    url = "https://whisky.auction/auctions/search?src=" + urllib.parse.quote(term)
    page = _get(url)
    out = []
    for card in page.split('<div class="lot-info-image"')[1:]:   # 로트 카드 하나의 시작
        m = re.search(r'href="/auctions/lot/(\d+)/([^"]+)"', card)
        if not m:
            continue
        name = re.search(r'class="lotName1 lot-title-main">(.*?)</span>', card, re.S)
        sup = re.findall(r'class="lotName\d lot-title-sup">(.*?)</span>', card, re.S)
        title = " ".join(html.unescape(re.sub(r"<[^>]+>", " ", x)).strip()
                         for x in ([name.group(1)] if name else []) + sup)
        title = re.sub(r"\s+", " ", title).strip()
        cur_bid = re.search(r'class="winningBid value">&pound;([\d,]+)', card)
        start = re.search(r'class="startingBid value">&pound;([\d,]+)', card)
        left = re.search(r'class="watch-count timer" data-left="(\d+)"', card)
        price = int((cur_bid or start).group(1).replace(",", "")) if (cur_bid or start) else None
        if cur_bid and price == 0 and start:
            price = int(start.group(1).replace(",", ""))
        out.append({
            "source": "whiskyauction", "id": m.group(1), "title": title or m.group(2).replace("-", " "),
            "url": f"https://whisky.auction/auctions/lot/{m.group(1)}/{m.group(2)}",
            "price": price, "cur": "GBP", "kind": "auction", "live": True,
            "end": int(time.time()) + int(left.group(1)) if left else None,
            "bids": None, "postage": 0, "buynow": None,
            "note": "시작가 기준일 수 있음 · 낙찰수수료 15%(영국 외 배송은 수수료 VAT 면제)",
        })
    return out


# ---------------------------------------------------------------- HTFW (Hard To Find Whisky, 영국 버밍엄)
_HTFW_CARD = re.compile(r'<a\s+href="(https://www\.htfw\.com/[^"]+)"\s+title="([^"]+)"\s+'
                        r'class="group item product product-item')


def htfw_parse(page, source="htfw"):
    """HTFW 목록 페이지(검색·신착) → 상품. 카드에는 가격이 없고, 페이지에 박힌 GA4 전자상거래
    데이터(dl4Objects의 ecommerce.items: item_name·item_id·price)에 있다 — 이름으로 짝지운다.
    품절은 카드 안의 'Out Of Stock' 문구로 판정(품절 카드는 회색 처리도 됨)."""
    ga = {}
    m = re.search(r"var dl4Objects = (\[.*?\]);", page, re.S)
    if m:
        try:
            for o in json.loads(m.group(1)):
                for x in (o.get("ecommerce", {}).get("items") or []):
                    ga[html.unescape(x.get("item_name", ""))] = x
        except ValueError:
            pass
    cards = list(_HTFW_CARD.finditer(page))
    out = []
    for n, c in enumerate(cards):
        body = page[c.end(): cards[n + 1].start() if n + 1 < len(cards) else c.end() + 8000]
        name = html.unescape(c.group(2))
        g = ga.get(name, {})
        out.append({
            "source": source, "id": str(g.get("item_id") or c.group(1).rsplit("/", 1)[-1]),
            "title": name, "url": c.group(1),
            "price": g.get("price"), "cur": "GBP", "kind": "retail",
            "live": "out of stock" not in body.lower(), "end": None,
            "bids": None, "postage": 0, "buynow": None, "note": "리테일 즉시구매",
        })
    return out


def htfw(term):
    page = _get("https://www.htfw.com/catalogsearch/result/?" + urllib.parse.urlencode({"q": term}))
    return [i for i in htfw_parse(page) if i["live"]]


# ---------------------------------------------------------------- Whisky-Maniac (독일)
def whiskymaniac_parse(page, source="whiskymaniac"):
    """상품마다 schema.org 마이크로데이터가 있다(sku·url·name·price·priceCurrency·availability)."""
    out = []
    for b in page.split('<div class="product-item" itemScope=""')[1:]:
        sku = re.search(r'itemProp="sku" content="([^"]+)"', b)
        url = re.search(r'class="product-item-link" href="([^"]+)"', b)
        name = re.search(r'itemProp="name">(.*?)</h2>', b, re.S)
        price = re.search(r'itemProp="price" content="([\d.]+)"', b)
        av = re.search(r'itemProp="availability" href="https://schema.org/(\w+)"', b)
        if not (sku and url and name):
            continue
        out.append({
            "source": source, "id": sku.group(1), "title": html.unescape(name.group(1)).strip(),
            "url": "https://www.whisky-maniac.de" + url.group(1),
            "price": float(price.group(1)) if price else None, "cur": "EUR", "kind": "retail",
            "live": (av.group(1) if av else "") == "InStock", "end": None,
            "bids": None, "postage": 0, "buynow": None, "note": "리테일 즉시구매",
        })
    return out


def whiskymaniac(term):
    # ?page=2 는 1~2페이지를 누적해서 준다(더보기 방식, 2026-09-25 실측)
    page = _get("https://www.whisky-maniac.de/search?" + urllib.parse.urlencode({"q": term, "page": 2}))
    return [i for i in whiskymaniac_parse(page) if i["live"]]


# ---------------------------------------------------------------- Nickolls & Perks (영국, WooCommerce)
NP_API = "https://nickollsandperks.com/wp-json/wc/store/products"


def nickolls_items(params, source="nickolls"):
    """WooCommerce Store API(구 네임스페이스 /wc/store — /wc/store/v1 은 401). 공개 JSON.
    ⚠️ 이 API의 가격은 **VAT 제외가**다(웹 £74.95 → API £62.46 = ÷1.2, 2026-09-25 실측) — 수출가로 그대로 쓴다.
    와인이 섞여 있으므로 신착은 위스키 카테고리(6723)로 거른다."""
    url = NP_API + "?" + urllib.parse.urlencode(params)
    for attempt in range(3):
        body = _get(url, accept="application/json")
        try:
            d = json.loads(body)
            break
        except ValueError:
            # 러너에서 가끔 JSON 대신 HTML(보호 페이지 추정)이 온다(2026-09-25) — 물러섰다 재시도
            if attempt == 2:
                raise RuntimeError("JSON 아님: " + re.sub(r"\s+", " ", body[:120]))
            time.sleep(3 * (attempt + 1))
    out = []
    for x in d:
        pr = x.get("prices") or {}
        try:
            price = int(pr.get("price")) / 10 ** int(pr.get("currency_minor_unit", 2))
        except (TypeError, ValueError):
            price = None
        out.append({
            "source": source, "id": str(x.get("id")), "title": html.unescape(x.get("name", "")),
            "url": x.get("permalink"), "price": price, "cur": pr.get("currency_code") or "GBP",
            "kind": "retail", "live": bool(x.get("is_in_stock")), "end": None,
            "bids": None, "postage": 0, "buynow": None, "note": "리테일 즉시구매 · 가격은 VAT 제외가",
        })
    return out


def nickolls(term):
    return [i for i in nickolls_items({"search": term, "per_page": 50}) if i["live"]]


# ---------------------------------------------------------------- Whiskysite.nl (네덜란드, Lightspeed) — 일본 배송만
def _is_small(title):
    """샘플·35cl 미만 소용량. 숫자는 통째로 읽어 값으로 비교한다 — 부분 매칭으로 보면 '70 CL'의
    '7'만 떼어 7cl로 오판한다(2026-09-25 실측)."""
    if re.search(r"\bsamples?\b", title, re.I):
        return True
    for m in re.finditer(r"(?<![\d.,])(\d+(?:[.,]\d+)?)\s?cl\b", title, re.I):
        if float(m.group(1).replace(",", ".")) < 35:
            return True
    return False


def whiskysite_items(path, params=None, source="whiskysite"):
    """Lightspeed 샵은 아무 목록 URL에 ?format=json 을 붙이면 구조화 JSON을 준다
    (collection.products — title·url·available·price.price_incl/price_excl). 6cl 샘플이 대량이라 거른다.
    ⚠️ 이 샵은 **일본만** 배송하고 한국은 배송 목록에 없다(2026-09-25 배송 페이지 확인)."""
    q = dict(params or {}, format="json")
    d = json.loads(_get(f"https://www.whiskysite.nl/en/{path}?" + urllib.parse.urlencode(q), accept="application/json"))
    prods = (d.get("collection") or {}).get("products") or {}
    out = []
    for v in prods.values():
        title = html.unescape(v.get("title") or "")
        if _is_small(title):
            continue
        pr = v.get("price") or {}
        out.append({
            "source": source, "id": str(v.get("id")), "title": title,
            "url": "https://www.whiskysite.nl/en/" + (v.get("url") or ""),
            "price": pr.get("price_incl") or pr.get("price"), "cur": "EUR", "kind": "retail",
            "live": bool(v.get("available")), "end": None,
            "bids": None, "postage": 0, "buynow": None, "note": "리테일 즉시구매 · 일본 배송만(한국 불가)",
        })
    return out


def whiskysite(term, pages=2):
    out = []
    for page in range(1, pages + 1):          # 24개/페이지인데 샘플이 섞여 1페이지만으론 부족하다
        got = whiskysite_items(f"search/{urllib.parse.quote(term)}/", {"page": page})
        out += [i for i in got if i["live"]]
    return out


# ---------------------------------------------------------------- La Maison du Whisky (프랑스, whisky.fr)
LMDW_GQL = "https://gateway.prod2.whisky.fr/graphql"
LMDW_WHISKY_UID = "NDExNw=="     # 'Les types de whiskies' 루트 카테고리(약 2,800종)
_LMDW_Q = ("query P($search:String,$f:ProductAttributeFilterInput!,$n:Int!,$sort:ProductAttributeSortInput)"
           "{products(search:$search filter:$f pageSize:$n currentPage:1 sort:$sort)"
           "{items{sku name in_stock url_key url_rewrites{url} price_range{minimum_price{final_price{currency value}}}}}}")


def lmdw_items(variables, source="lmdw"):
    """검색 결과는 브라우저에서 JS로 그려지고 HTML엔 상품이 없다 — 실제 원천은 Magento GraphQL
    게이트웨이(GET)다(2026-09-25 브라우저 네트워크에서 확인). 신착은 sort lmdw_activation_date DESC."""
    u = LMDW_GQL + "?" + urllib.parse.urlencode({"query": _LMDW_Q, "operationName": "P",
                                                 "variables": json.dumps(variables)})
    d = json.loads(_get(u, accept="application/json"))
    if d.get("errors"):
        raise RuntimeError(d["errors"][0].get("message", "graphql error")[:200])
    out = []
    for x in (d.get("data") or {}).get("products", {}).get("items", []):
        fp = (((x.get("price_range") or {}).get("minimum_price") or {}).get("final_price") or {})
        path = ((x.get("url_rewrites") or [{}])[0] or {}).get("url") or f"{x.get('url_key')}.html"
        out.append({
            "source": source, "id": str(x.get("sku")), "title": html.unescape(x.get("name") or ""),
            "url": "https://www.whisky.fr/en/" + path,
            "price": fp.get("value"), "cur": fp.get("currency") or "EUR", "kind": "retail",
            "live": bool(x.get("in_stock")), "end": None,
            "bids": None, "postage": 0, "buynow": None, "note": "리테일 즉시구매",
        })
    return out


def lmdw(term):
    return [i for i in lmdw_items({"search": term, "f": {}, "n": 50}) if i["live"]]


# fee: 낙찰가·판매가에 곱할 계수(구매대행·낙찰수수료). ship: 일본까지 배송비(그 통화 기준).
# 2026-09-23 각 사이트 배송·수수료 페이지에서 확인한 값. 영국 옥션은 전부 DHL 지정이고
# 일본 측 주세·소비세·통관비는 수취인 부담이다(어디서 사든 동일).
REGISTRY = {
    "yahoo": {"fn": yahoo, "label": "야후옥션", "script": "ja", "cur": "JPY",
              "fee": 0.05, "ship": None, "fee_note": "대행수수료 5%"},
    "swa": {"fn": swa, "label": "SWA(영국옥션)", "script": "en", "cur": "GBP",
            "fee": 0.18, "ship": None, "fee_note": "수수료 15%+수수료VAT, 배송 개별견적"},
    "justwhisky": {"fn": justwhisky, "label": "Just Whisky(영국옥션)", "script": "en", "cur": "GBP",
                   "fee": 0.125, "ship": 69, "fee_note": "수수료 12.5%(영국 외 VAT 없음)+배송 £69"},
    "whiskyauction": {"fn": whiskyauction, "label": "Whisky.Auction(영국옥션)", "script": "en", "cur": "GBP",
                      "fee": 0.15, "ship": 37, "fee_note": "수수료 15%+배송 £37"},
    # 리테일: vat = 표시가에 들어 있고 수출 시 빠지는 부가세율(표시가 ÷ (1+vat)).
    # 영국 샵의 한국·일본 마켓 가격이 기준가의 약 ÷1.2로 나오는 것을 확인했다(2026-09-25).
    # ⚠️ 이전 WIO 계산은 ×0.8(−20%)이었는데 VAT 포함가에서 20%를 빼는 건 ÷1.2(−16.7%)가 맞다.
    "wio": {"fn": wio, "label": "WIO(영국리테일)", "script": "en", "cur": "GBP",
            "fee": 0, "vat": 0.20, "ship": 33, "fee_note": "영국 VAT 제외+배송 £33"},
    "whiskybarrel": {"fn": shopify("https://www.thewhiskybarrel.com", "whiskybarrel", "GBP"),
                     "label": "The Whisky Barrel(영국)", "script": "en", "cur": "GBP",
                     "fee": 0, "vat": 0.20, "ship": 44, "fee_note": "영국 VAT 제외+배송 £44(안내가)"},
    "innout": {"fn": shopify("https://inn-out-shop.com", "innout", "EUR"),
               "label": "Inn-Out(독일)", "script": "en", "cur": "EUR",
               "fee": 0, "vat": 0.19, "ship": 49.99, "fee_note": "독일 VAT 제외+배송 €49.99(아시아 1~5kg)"},
    "topwhiskies": {"fn": shopify("https://topwhiskies.com", "topwhiskies", "GBP"),
                    "label": "Top Whiskies(영국)", "script": "en", "cur": "GBP",
                    "fee": 0, "vat": 0.20, "ship": None, "fee_note": "영국 VAT 제외, 배송 결제 시 산정"},
    "abbey": {"fn": shopify("https://www.abbeywhisky.com", "abbey", "GBP"),
              "label": "Abbey Whisky(영국)", "script": "en", "cur": "GBP",
              "fee": 0, "vat": 0.20, "ship": None, "fee_note": "영국 VAT 제외, 배송(DHL) 결제 시 산정"},
    "reallygood": {"fn": shopify("https://reallygoodwhisky.com", "reallygood", "GBP"),
                   "label": "Really Good Whisky(영국)", "script": "en", "cur": "GBP",
                   "fee": 0, "vat": 0.20, "ship": None, "fee_note": "영국 VAT 제외, 배송 결제 시 산정(직배 여부 주문 전 확인)"},
    # HTML 샵: VAT 차감이 확인 안 된 곳은 vat=0(보수적) — 추정가를 낮게 잡아 🎯를 잘못 붙이지 않도록.
    "htfw": {"fn": htfw, "label": "HTFW(영국)", "script": "en", "cur": "GBP",
             "fee": 0, "vat": 0, "ship": None, "fee_note": "배송 결제 시 산정·무료보험, VAT 차감 미확인(미반영)"},
    "whiskymaniac": {"fn": whiskymaniac, "label": "Whisky-Maniac(독일)", "script": "en", "cur": "EUR",
                     "fee": 0, "vat": 0, "ship": 40, "fee_note": "세계배송 €40~(DHL), VAT 차감 미확인(미반영)"},
    "nickolls": {"fn": nickolls, "label": "Nickolls & Perks(영국)", "script": "en", "cur": "GBP",
                 "fee": 0, "vat": 0, "ship": None, "fee_note": "표시가가 이미 VAT 제외가, 배송 결제 시 산정"},
    "whiskysite": {"fn": whiskysite, "label": "Whiskysite.nl(네덜란드·일본배송만)", "script": "en", "cur": "EUR",
                   "fee": 0, "vat": 0, "ship": None, "fee_note": "일본만 배송, 배송비 결제 시 산정, VAT 차감 미확인(미반영)"},
    "lmdw": {"fn": lmdw, "label": "La Maison du Whisky(프랑스)", "script": "en", "cur": "EUR",
             "fee": 0, "vat": 0, "ship": 20, "fee_note": "존5(한·일) 배송 €20~, 레어 병은 DHL, VAT 차감 미확인(미반영)"},
}
