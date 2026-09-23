#!/usr/bin/env python3
"""
auction_watch.py — 원하는 보틀을 경매·리테일 사이트에서 찾아 텔레그램으로 알린다 (토큰 0)

감시 대상 두 갈래:
  1. auction_watchlist.json — 직접 적어 넣은 항목(all_of/none_of 규칙을 손으로 지정)
  2. 노션 위스키 리스트에서 **추적=✓ 인 행 전부** — 텔레그램 `/add`로 추가한 보틀이 그대로 들어온다.
     매칭 규칙은 `추적 키워드`(없으면 상품명 정리본)를 brands.json으로 다국어 확장해 자동 생성.

감시 사이트: 야후옥션(일본) · Scotch Whisky Auctions(영국) · Just Whisky(영국) · WIO(영국 리테일).
영국 3곳은 모두 일본 배송이 가능한 곳으로 골랐다(일본 수령 후 휴대 반입이 세금상 최선).
수집 세부와 사이트별 함정은 sources.py 참고.

알림: 🆕 신규 매물 · ⏰ 종료 임박(야후) · 📉 리테일 가격 하락 · 🎯 목표가 진입
상태: data/auction_seen.json   사용: --dry(알림 없이 목록) / --report(현재 매물 전체 1회 보고)
"""
import argparse, json, re, urllib.error
from datetime import datetime
from pathlib import Path

import bottle_tracker as bt
import sources
import tg
import whisky_watch

DATA = bt.DATA
SEEN = DATA / "auction_seen.json"
OLD_SEEN = DATA / "yahoo_seen.json"          # 야후 전용 시절 상태 — 한 번만 흡수한다
STATE = DATA / "tracker_state.json"
CONF = Path(__file__).with_name("auction_watchlist.json")

SOON_H = 3          # 종료 몇 시간 전에 임박 알림을 보낼지 (종료시각을 아는 야후만)
DROP = 0.03         # 리테일 가격 하락 알림 기준


def log(msg):
    bt.log(f"[AUC] {msg}")


# ---------------------------------------------------------------- 감시 대상 만들기
def _script_of(text):
    if re.search(r"[぀-ヿ一-鿿]", text):
        return "ja"
    if re.search(r"[가-힯]", text):
        return "ko"
    return "en"


def _term_for(alias, script):
    """별칭을 그 사이트 언어에 맞는 검색어 하나로. 브랜드는 brands.json으로 표기 변환."""
    picked = []
    for variants, _w in bt.expand_query(alias):
        same = [v for v in variants if _script_of(v) == script]
        picked.append((same or variants)[0])
    return " ".join(picked[:3]) if picked else alias


def targets_from_conf():
    out = []
    for q in bt.load_json(CONF, {}).get("queries", []):
        out.append({
            "name": q["name"],
            "rule": {"all_of": q.get("all_of", []), "none_of": q.get("none_of", [])},
            "ages": [],
            "terms": {"yahoo": q.get("search", []),
                      **{k: q.get("search_en") or q.get("search", []) for k in ("swa", "justwhisky", "wio")}},
            "target_jpy": q.get("target_jpy"), "target_krw": q.get("target_krw"),
            "sources": q.get("sources") or list(sources.REGISTRY),
        })
    return out


def targets_from_notion():
    """추적=✓ 행 → 감시 대상. 규칙은 별칭을 다국어로 펼친 all_of(그룹 간 AND, 그룹 내 OR)."""
    try:
        rows = [bt.parse_page(p) for p in bt.notion_query_tracked()]
    except Exception as e:
        log(f"노션 조회 실패 — 설정 파일 항목만 감시: {e}")
        return []
    out = []
    for b in rows:
        aliases = bt.alias_list(b)
        if not aliases:
            continue
        alias = aliases[0]
        groups = [vs for vs, _w in bt.expand_query(alias)]
        if not groups:
            continue
        out.append({
            "name": b["name"],
            "rule": {"all_of": groups, "none_of": ["空瓶", "空き瓶", "ミニチュア", "ミニボトル",
                                                   "50ml", "100ml", "箱のみ", "ラベルのみ",
                                                   "empty bottle", "miniature", "5cl"]},
            # 별칭에 연식이 없으면 상품명에서 뽑는다 — 'Chapter 23' 같은 건 query_ages가 알아서 제외
            "ages": bt.query_ages(alias) or bt.query_ages(b["name"]),
            "terms": {k: [_term_for(alias, v["script"])] for k, v in sources.REGISTRY.items()},
            "target_jpy": None, "target_krw": b.get("target_krw"),
            "sources": list(sources.REGISTRY),
        })
    return out


def matches(title, target):
    low = title.lower()
    rule = target["rule"]
    if any(bad.lower() in low for bad in rule.get("none_of", [])):
        return False
    if target.get("ages") and bt.age_conflict(title, target["ages"]):
        return False
    return all(any(tok.lower() in low for tok in group) for group in rule.get("all_of", []))


# ---------------------------------------------------------------- 수집·판정
def collect(target):
    found, seen_ids = [], set()
    for key in target["sources"]:
        src = sources.REGISTRY.get(key)
        if not src:
            continue
        for term in target["terms"].get(key, []):
            if not term:
                continue
            try:
                items = src["fn"](term)
            except Exception as e:
                log(f"{src['label']} 검색 실패 '{term}': {type(e).__name__} {e}")
                continue
            for it in items:
                k = f"{it['source']}:{it['id']}"
                if k in seen_ids or not matches(it["title"], target):
                    continue
                seen_ids.add(k)
                found.append(it)
    return found


def landed(it, rates):
    """일본 수령 기준 추정 총액(원) — 사이트별 수수료·일본 배송비까지 포함.
    한국까지는 **휴대 반입 전제**다. 정식통관하면 여기에 관세·주세·교육세·부가세로 약 2.55배가 더 붙는다.
    WIO는 영국 외 배송이면 VAT 20%가 빠지므로 fee가 음수(-0.20)다."""
    if it["price"] is None:
        return None
    src = sources.REGISTRY[it["source"]]
    base = (it["price"] + it.get("postage", 0)) * (1 + src["fee"])
    if src.get("ship"):
        base += src["ship"]
    return bt.to_krw(base, it["cur"], rates)


def fmt(it, target, rates):
    label = sources.REGISTRY[it["source"]]["label"]
    head = f"<b>{tg.esc(it['title'])}</b>\n{label}"
    lines = [head]
    if it["price"] is not None:
        sym = "¥" if it["cur"] == "JPY" else "£"
        line = f"현재가 {sym}{it['price']:,}"
        if it.get("postage"):
            line += f" +송료 ¥{it['postage']:,}"
        krw = bt.to_krw(it["price"], it["cur"], rates)
        if krw:
            line += f" ≈ {krw:,}원"
        lines.append(line)
        tot = landed(it, rates)
        if tot:
            src = sources.REGISTRY[it["source"]]
            tail = src["fee_note"] + ("" if src.get("ship") or it["source"] != "yahoo" else ", 국제배송료 별도")
            lines.append(f"일본 수령 기준 추정 {tot:,}원 ({tail})")
    tgt_krw = target.get("target_krw")
    tot = landed(it, rates)
    if tgt_krw and tot and tot <= tgt_krw:
        lines.append(f"🎯 목표가 {tgt_krw:,}원 이하")
    if target.get("target_jpy") and it["cur"] == "JPY" and it["price"] and it["price"] <= target["target_jpy"]:
        lines.append(f"🎯 목표가 ¥{target['target_jpy']:,} 이하")
    tail = []
    if it.get("bids") is not None:
        tail.append(f"입찰 {it['bids']}건")
    if it.get("end"):
        end = datetime.fromtimestamp(it["end"], bt.KST)
        left = int((end - bt.now()).total_seconds() // 3600)
        tail.append(f"종료 {end.strftime('%m/%d %H:%M')} KST" + (f" (남은 {left}시간)" if left > 0 else " (곧 종료)"))
    if it.get("note"):
        tail.append(it["note"])
    if tail:
        lines.append(tg.esc(" · ".join(tail)))
    lines.append(it["url"])
    return "\n".join(lines)


# ---------------------------------------------------------------- 실행
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true", help="알림 없이 매치 목록만 출력")
    ap.add_argument("--report", action="store_true", help="현재 매치 전체를 텔레그램으로 1회 보고")
    ap.add_argument("--only", help="이 이름이 들어간 감시 대상만")
    args = ap.parse_args()

    seen = bt.load_json(SEEN, None)
    if seen is None:                                   # 야후 전용 시절 상태를 한 번 흡수
        seen = {f"yahoo:{k}": v for k, v in bt.load_json(OLD_SEEN, {}).items()}
    state = bt.load_json(STATE, {})
    chat = state.get("chat_id")
    rates = bt.fx_rates({})
    ts = int(bt.now().timestamp())
    alerts, total = [], 0

    targets = targets_from_conf() + targets_from_notion()
    if args.only:
        targets = [t for t in targets if args.only.lower() in t["name"].lower()]
    log(f"감시 대상 {len(targets)}건 · 사이트 {len(sources.REGISTRY)}곳")

    for target in targets:
        items = collect(target)
        total += len(items)
        log(f"{target['name']}: 매치 {len(items)}건")
        for it in items:
            key = f"{it['source']}:{it['id']}"
            rec = seen.get(key, {"alerts": []})
            done = rec.get("alerts", [])
            if args.dry:
                print(f"  [{it['source']}] {it['cur']}{it['price']} {it['title'][:60]}")
                continue
            head = None
            if "new" not in done:
                done.append("new")
                head = f"🆕 신규 매물 — {target['name']}"
            elif it.get("end") and 0 < it["end"] - ts <= SOON_H * 3600 and "soon" not in done:
                done.append("soon")
                head = f"⏰ 종료 {SOON_H}시간 이내 — {target['name']}"
            elif (it["kind"] == "retail" and it["price"] and rec.get("price")
                  and it["price"] <= rec["price"] * (1 - DROP)):
                head = f"📉 가격 하락 (£{rec['price']:,}→£{it['price']:,}) — {target['name']}"
            elif args.report:
                head = f"📋 현재 매물 — {target['name']}"
            if head:
                alerts.append((head, fmt(it, target, rates)))
            seen[key] = {"title": it["title"], "price": it["price"], "cur": it["cur"],
                         "end": it.get("end"), "alerts": done, "ts": ts}

    if args.dry:
        print(f"총 {total}건")
        return

    seen = {k: v for k, v in seen.items()
            if (v.get("end") or 0) > ts - 3 * 86400 or v.get("ts", 0) > ts - 30 * 86400}
    SEEN.parent.mkdir(parents=True, exist_ok=True)
    SEEN.write_text(json.dumps(seen, ensure_ascii=False, indent=1))

    if not alerts:
        log(f"변화 없음 (매치 {total}건)")
        return
    if chat:
        for head, body in alerts:
            tg.send(chat, f"{head}\n\n{body}")
        log(f"텔레그램 알림 {len(alerts)}건 발송")
    else:
        log("알림 있으나 텔레그램 chat_id 미등록 — 봇에 /start 를 보내야 함")
    send_mail(alerts)


def send_mail(alerts):
    """텔레그램과 같은 내용을 메일로도 한 통에 묶어 보낸다(수신자: watchlist.json notify.email).
    HTML 태그는 빼고 평문으로. 메일 설정이 없으면 whisky_watch.send_email이 조용히 넘어간다."""
    to = (bt.load_json(bt.WATCHLIST, {}).get("notify") or {}).get("email")
    if not to:
        return
    strip = lambda t: re.sub(r"<[^>]+>", "", t).replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    body = "\n\n".join(f"{strip(h)}\n{strip(b)}" for h, b in alerts)
    subject = strip(alerts[0][0]) if len(alerts) == 1 else f"위스키 경매 알림 {len(alerts)}건"
    whisky_watch.send_email(f"[위스키] {subject}", body, to)
    log(f"메일 발송 → {to}")


if __name__ == "__main__":
    main()
