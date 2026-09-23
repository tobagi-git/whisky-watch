#!/usr/bin/env python3
"""
yahoo_watch.py — 야후옥션(ヤフオク) 키워드 감시 → 신규 매물·종료 임박을 텔레그램으로 알린다 (토큰 0)

whisky_watch.py(상점 폴링)·bottle_tracker.py(노션 추적)와 같은 워크플로에서 돌고,
텔레그램 chat_id는 tracker_state.json에 이미 등록된 값을 그대로 쓴다.

수집: auctions.yahoo.co.jp 검색결과 HTML의 data-auction-* 속성
      (id / title / price(표시가=스토어는 税込) / buynowprice / endtime / 입찰수 / 송료)
감시 항목: yahoo_watchlist.json  — all_of(그룹 간 AND, 그룹 내 OR) + none_of 로 오탐 제거.
          "ハウスリザーブ"만으로 검색하면 무관한 병이 걸리므로 반드시 브랜드 그룹과 AND로 묶는다.
상태: data/yahoo_seen.json  {auction_id: {title, price, end, alerts:[...]}} — 종료 3일 뒤 정리

사용: python3 yahoo_watch.py          평상시(신규·임박만 알림)
      python3 yahoo_watch.py --dry    알림 없이 현재 매치 목록만 출력
      python3 yahoo_watch.py --report 현재 매치 전체를 텔레그램으로 1회 보고(상태 점검용)
"""
import argparse, html, json, re, urllib.error, urllib.parse, urllib.request
from datetime import datetime
from pathlib import Path

import tg
from bottle_tracker import DATA, KST, fx_rates, load_json, log as _log, now, to_krw

SEEN = DATA / "yahoo_seen.json"
STATE = DATA / "tracker_state.json"
CONF = Path(__file__).with_name("yahoo_watchlist.json")
SEARCH = "https://auctions.yahoo.co.jp/search/search"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/140.0 Safari/537.36")
SOON_H = 3          # 종료 몇 시간 전에 임박 알림을 보낼지
AGENT_FEE = 0.05    # 구매대행 수수료(카에루몰 기준 5%) — 원화 총액 추정용


def log(msg):
    _log(msg.replace("[TRK] ", ""))


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "ja,en;q=0.8"})
    with urllib.request.urlopen(req, timeout=25) as r:
        return r.read().decode("utf-8", errors="replace")


def search(q, pages=3, n=100):
    """검색어 하나로 최대 pages장.

    ⚠️ 야후는 '결과 0건'을 404로 돌려준다(2026-09-23 실측, 인코딩과 무관). 그래서 여러 단어를
    AND로 넣으면 결과가 없을 때 예외처럼 보인다 — 광역 단어 하나로 긁고 all_of/none_of로 거른다.
    """
    out = []
    for page in range(pages):
        qs = urllib.parse.urlencode({"p": q, "n": n, "b": page * n + 1, "s1": "new", "o1": "d"})
        try:
            items = parse(fetch(f"{SEARCH}?{qs}"))
        except urllib.error.HTTPError as e:
            if e.code == 404:      # 결과 없음
                break
            raise
        out += items
        if len(items) < n:
            break
    return out


def parse(page):
    """검색결과 HTML → 매물 dict 리스트."""
    out = []
    for blk in page.split('<li class="Product">')[1:]:
        m = re.search(r'data-auction-id="([^"]+)"', blk)
        t = re.search(r'data-auction-title="([^"]*)"', blk)
        if not (m and t):
            continue
        num = lambda pat: int(re.search(pat, blk).group(1)) if re.search(pat, blk) else None
        postage = 0
        pm = re.search(r'Product__postage[^>]*>\s*＋?送料([\d,]+)円', blk)
        if pm:
            postage = int(pm.group(1).replace(",", ""))
        bids = re.search(r'Product__bid">(\d+)<', blk)
        out.append({
            "id": m.group(1),
            "title": html.unescape(t.group(1)),
            "price": num(r'data-auction-price="(\d+)"'),
            "buynow": num(r'data-auction-buynowprice="(\d+)"') or None,
            "end": num(r'data-auction-endtime="(\d+)"'),
            "postage": postage,
            "bids": int(bids.group(1)) if bids else 0,
            "store": 'data-auction-isshoppingitem="1"' in blk,
            "url": f"https://auctions.yahoo.co.jp/jp/auction/{m.group(1)}",
        })
    return out


def matches(title, rule):
    low = title.lower()
    for bad in rule.get("none_of", []):
        if bad.lower() in low:
            return False
    for group in rule.get("all_of", []):
        if not any(tok.lower() in low for tok in group):
            return False
    return True


def collect(rule):
    """한 감시 항목의 검색어들을 돌려 매치된 매물을 id 기준으로 합친다."""
    found, seen_ids = [], set()
    for q in rule["search"]:
        try:
            items = search(q)
        except Exception as e:
            log(f"[YHO] 검색 실패 '{q}': {e}")
            continue
        for it in items:
            if it["id"] in seen_ids or not matches(it["title"], rule):
                continue
            seen_ids.add(it["id"])
            found.append(it)
    return found


def fmt(it, rates, rule):
    """알림 1건 본문."""
    end = datetime.fromtimestamp(it["end"], KST) if it["end"] else None
    left = (end - now()) if end else None
    price, post = it["price"] or 0, it["postage"]
    krw = to_krw(price, "JPY", rates)
    total_jpy = round((price + post) * (1 + AGENT_FEE))
    lines = [f"<b>{tg.esc(it['title'])}</b>",
             f"현재가 ¥{price:,}" + (f" +송료 ¥{post:,}" if post else " (송료무료)")
             + (f"  ≈ {krw:,}원" if krw else ""),
             f"대행 총액 추정 ¥{total_jpy:,} ≈ {to_krw(total_jpy, 'JPY', rates):,}원 "
             f"(수수료 {int(AGENT_FEE*100)}%, 국제배송료 별도)"]
    if it["buynow"]:
        lines.append(f"즉결 ¥{it['buynow']:,} ≈ {to_krw(it['buynow'], 'JPY', rates):,}원")
    tgt = rule.get("target_jpy")
    if tgt and price <= tgt:
        lines.append(f"🎯 목표가 ¥{tgt:,} 이하")
    tail = f"입찰 {it['bids']}건"
    if it["store"]:
        tail += " · 스토어(税込 표시)"
    if end:
        h = int(left.total_seconds() // 3600) if left and left.total_seconds() > 0 else 0
        tail += f" · 종료 {end.strftime('%m/%d %H:%M')} KST"
        tail += f" (남은 {h}시간)" if h else " (곧 종료)"
    lines.append(tail)
    lines.append(it["url"])
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true", help="알림 없이 매치 목록만 출력")
    ap.add_argument("--report", action="store_true", help="현재 매치 전체를 텔레그램으로 1회 보고")
    args = ap.parse_args()

    conf = load_json(CONF, {"queries": []})
    seen = load_json(SEEN, {})
    state = load_json(STATE, {})
    chat = state.get("chat_id")
    rates = fx_rates({})
    ts = int(now().timestamp())
    alerts, total = [], 0

    for rule in conf.get("queries", []):
        items = collect(rule)
        total += len(items)
        log(f"[YHO] {rule['name']}: 매치 {len(items)}건")
        for it in items:
            rec = seen.get(it["id"], {"alerts": []})
            done = rec.get("alerts", [])
            if args.dry:
                print(f"  {it['title']}  ¥{it['price']:,}  {it['url']}")
                continue
            if "new" not in done:
                alerts.append(("🆕 야후옥션 신규 매물 — " + rule["name"], fmt(it, rates, rule)))
                done.append("new")
            elif it["end"] and 0 < it["end"] - ts <= SOON_H * 3600 and "soon" not in done:
                alerts.append((f"⏰ 종료 {SOON_H}시간 이내 — " + rule["name"], fmt(it, rates, rule)))
                done.append("soon")
            elif args.report:
                alerts.append(("📋 현재 매물 — " + rule["name"], fmt(it, rates, rule)))
            seen[it["id"]] = {"title": it["title"], "price": it["price"],
                              "end": it["end"], "alerts": done}

    if args.dry:
        print(f"총 {total}건")
        return

    # 종료 3일 지난 건 정리
    seen = {k: v for k, v in seen.items() if not v.get("end") or v["end"] > ts - 3 * 86400}
    SEEN.parent.mkdir(parents=True, exist_ok=True)
    SEEN.write_text(json.dumps(seen, ensure_ascii=False, indent=1))

    if not alerts:
        log(f"[YHO] 변화 없음 (매치 {total}건)")
        return
    if not chat:
        log("[YHO] 알림 있으나 텔레그램 chat_id 미등록 — 봇에 /start 를 보내야 함")
        return
    for head, body in alerts:
        tg.send(chat, f"{head}\n\n{body}")
    log(f"[YHO] 텔레그램 알림 {len(alerts)}건 발송")


if __name__ == "__main__":
    main()
