"""야후옥션이 어떤 조건에서 막히는지 러너에서 확인하는 일회성 프로브."""
import gzip, json, urllib.error, urllib.parse, urllib.request

CHROME = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/140.0 Safari/537.36")
IPHONE = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 "
          "(KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1")
FULL = {
    "User-Agent": CHROME,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate",
    "Referer": "https://auctions.yahoo.co.jp/",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document", "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "same-origin", "Sec-Fetch-User": "?1",
}
q = urllib.parse.urlencode({"p": "アードベッグ", "n": 20})
CASES = [
    ("minimal UA",   f"https://auctions.yahoo.co.jp/search/search?{q}", {"User-Agent": CHROME}),
    ("full headers", f"https://auctions.yahoo.co.jp/search/search?{q}", FULL),
    ("iphone UA",    f"https://auctions.yahoo.co.jp/search/search?{q}", {**FULL, "User-Agent": IPHONE}),
    ("closedsearch", f"https://auctions.yahoo.co.jp/closedsearch/closedsearch?{q}", FULL),
    ("category",     f"https://auctions.yahoo.co.jp/category/list/2084005252/?{q}", FULL),
    ("top page",     "https://auctions.yahoo.co.jp/", FULL),
    ("ipinfo",       "https://ipinfo.io/json", {"User-Agent": "curl/8"}),
]
for name, url, h in CASES:
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=25) as r:
            raw = r.read()
            if r.headers.get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
            body = raw.decode("utf-8", "replace")
            extra = body[:200].replace("\n", " ") if name == "ipinfo" else f'items={body.count(chr(60)+"li class=" + chr(34) + "Product" + chr(34))}'
            print(f"{name:14} {r.status} len={len(body):>7} {extra}")
    except urllib.error.HTTPError as e:
        print(f"{name:14} HTTP {e.code} {e.reason}")
    except Exception as e:
        print(f"{name:14} ERR {e}")
