#!/usr/bin/env python3
"""
shop_digest.py — 감시 샵들의 위스키 신제품·재입고를 하루 두 번(08·19시 KST) 모아 보내는 '샵 리포트'.

관심 보틀 감시(auction_watch.py)와는 별개 리포트다. 즉시 키워드 알림(whisky_watch.py)도 그대로 둔다.

재료: whisky_watch.py가 매 폴링마다 data/shop_events.jsonl 에 쌓는 이벤트
      (new = 샵에 새로 올라온 상품 / restock = 품절 → 재고로 바뀐 상품)
      08·19시 직전 실행은 --deep(카탈로그 전체) 스캔이라 오래된 상품의 재입고도 잡힌다.
필터: 위스키만 — 샵 분류값(hint) 우선, 없으면 제목(위스키 단어·증류소/병입자명·연식 vs 진·럼 등).
      샘플·35cl 미만 소용량, 굿즈(잔·기프트카드 등)는 뺀다.
발송: 텔레그램 = ⭐(watchlist 키워드 매치) 목록 + 샵별 건수 요약 / 메일 = 전체 목록.
상태: data/digest_state.json(last_sent). 7일 지난 이벤트는 정리한다.

사용: python3 shop_digest.py --auto    # 워크플로 매 실행마다 호출 — 발송 시각이 지났고 아직 안 보냈을 때만 발송
      python3 shop_digest.py           # 즉시 발송
      python3 shop_digest.py --dry     # 발송 없이 출력만 (상태도 안 바꿈)

⚠️ --auto 인 이유: GitHub Actions는 같은 concurrency 그룹에 대기 중인 실행이 있으면 새 실행이 들어올 때
   대기 중인 쪽을 취소한다. 08·19시용 전용 실행(Worker가 07:55·18:55에 --deep 으로 dispatch)이 텔레그램
   명령 등으로 밀려 취소돼도, 그다음 아무 실행이 리포트를 대신 보내도록 '시각 도래 + 미발송'으로 판정한다.
"""
import argparse, json, re
from collections import OrderedDict
from datetime import datetime, timedelta

import bottle_tracker as bt
import sources
import tg
import whisky_watch

DATA = bt.DATA
EVENTS = DATA / "shop_events.jsonl"
STATE = DATA / "digest_state.json"
SLOTS = [(7, 50), (18, 50)]      # 이 시각부터 발송 대상(08:00·19:00 리포트 — 07:55·18:55 정밀 스캔 실행이 보낸다)
TRACKER_STATE = DATA / "tracker_state.json"

SHOP_LABEL = {
    "rudder": "RUDDER 🇯🇵", "mukawa": "무카와 🇯🇵", "shinanoya": "시나노야 🇯🇵", "deinwhisky": "DeinWhisky 🇩🇪",
    "vitalaus": "비탈라우스", "whiskybarrel": "The Whisky Barrel 🇬🇧", "innout": "Inn-Out 🇩🇪",
    "topwhiskies": "Top Whiskies 🇬🇧", "abbey": "Abbey Whisky 🇬🇧", "reallygood": "Really Good 🇬🇧",
    "wio": "WIO 🇬🇧", "htfw": "HTFW 🇬🇧", "whiskymaniac": "Whisky-Maniac 🇩🇪", "nickolls": "Nickolls & Perks 🇬🇧",
    "whiskysite": "Whiskysite 🇳🇱(일본만)", "lmdw": "La Maison du Whisky 🇫🇷",
    "dailyshot_cvs": "데일리샷 CU·이마트24 픽업", "lotteon": "롯데ON 스마트픽",
}
SYM = {"JPY": "¥", "EUR": "€", "GBP": "£", "KRW": "₩", "USD": "$"}

# ---------------------------------------------------------------- 위스키 판별
# 강한 위스키 신호(이게 있으면 제목에 rum·gin 이 있어도 위스키 — 'rum cask', 'gin cask' 피니시 등)
WHISKY_RX = re.compile(r"whisk(e)?y|scotch|bourbon|single\s*malt|\bmalt\b|\brye\b|single\s*cask|"
                       r"\bpeat(ed|y)?\b|\bislay\b|speyside|\bhighlands?\b|\blowlands?\b|campbeltown|"
                       r"cask\s*strength|blended\s*(malt|scotch)|\bgrain\b|"
                       r"ウイスキー|ウィスキー|モルト|バーボン|スコッチ|ピート|ピーテッド|アイラ|スペイサイド|"
                       r"シングルカスク|ブレンデッド|グレーン|위스키", re.I)
NON_RX = re.compile(r"\b(gin|rum|rhum|ron|vodka|cognac|armagnac|calvados|brandy|wine|vin|wein|champagne|"
                    r"liqueur|lik(ö|oe)r|tequila|mezcal|sake|beer|bier|cider|vermouth|grappa|pisco|absinthe|"
                    r"mead|soju|baijiu|amaro|aperitif|umeshu)\b|ジン|ラム|ウォッカ|ブランデー|コニャック|焼酎|日本酒|"
                    r"ワイン|リキュール|テキーラ|梅酒|アマーロ", re.I)
MERCH_RX = re.compile(r"gift\s*card|voucher|gutschein|\bglass(es)?\b|glencairn|t-shirt|hoodie|\bcap\b|"
                      r"book\b|buch\b|decanter\s*only|empty|leerflasche|advent\s*calendar|adventskalender|"
                      r"tasting\s*(set|pack)|probierset|miniature\s*set|sample\s*set|cocktail|premix|\brtd\b|"
                      r"water\s*jug|グラス", re.I)

# 제목에 '위스키'란 말이 없어도 위스키로 볼 이름들(증류소·병입자·대표 브랜드). brands.json 과 합친다.
NAMES = """
aberfeldy aberlour allt-a-bhainne ardbeg ardmore ardnamurchan arran auchentoshan auchroisk aultmore balblair
balmenach balvenie ben nevis benriach benrinnes benromach bladnoch blair athol bowmore braeval brora
bruichladdich bunnahabhain caol ila caperdonich cardhu clynelish convalmore cragganmore craigellachie
daftmill dailuaine dalmore dalwhinnie deanston dufftown edradour fettercairn glen elgin glen garioch
glen grant glen keith glen moray glen ord glen scotia glen spey glenallachie glenburgie glencadam glendronach
glendullan glenfarclas glenfiddich glenglassaugh glengoyne glenkinchie glenlivet glenlossie glenmorangie
glenrothes glentauchers glenturret glenugie glenury highland park imperial inchgower inverleven jura kilchoman
kilkerran kininvie knockando knockdhu lagavulin laphroaig ledaig linkwood littlemill loch lomond lochside
longmorn longrow macallan macduff mannochmore miltonduff mortlach north port oban octomore pittyvaich
port charlotte port ellen pulteney rosebank roseisle royal brackla royal lochnagar scapa speyburn springbank
strathisla strathmill talisker tamdhu tamnavulin teaninich tobermory tomatin tomintoul tormore tullibardine
hazelburn inchmurrin craiglodge croftengea wolfburn kingsbarns lindores ncnean nc'nean torabhaig raasay
ardnahoe annandale isle of harris ballindalloch port askaig smokehead
an cnoc milk & honey orkney secret speyside secret highland
yamazaki hakushu hibiki chichibu karuizawa hanyu yoichi miyagikyo nikka suntory mars komagatake tsunuki
akkeshi nagahama shizuoka kanosuke asaka sakurao
buffalo trace weller stagg blanton eagle rare pappy van winkle wild turkey russell's booker's michter's
elijah craig heaven hill four roses maker's mark woodford reserve jack daniel's old forester 1792 larceny
knob creek jim beam whistlepig sazerac eh taylor e.h. taylor old fitzgerald
kavalan amrut paul john mackmyra starward redbreast midleton green spot teeling bushmills
signatory cadenhead gordon & macphail g&m douglas laing hunter laing elixir distillers that boutique-y
decadent drinks whisky agency tbwc smws scotch malt whisky society adelphi blackadder berry bros
maltman malts of scotland wu dram clan sansibar kingsbury dramfool thompson bros old particular
first editions artful dodger whiskyland mackillop
""".replace("\n", " ")


def _names():
    # 공백 하나로 이어 쓴 목록이라 알려진 여러 단어 이름을 우선 보존하고 나머지는 단어로 쪼갠다
    words = set()
    multi = ["allt-a-bhainne", "ben nevis", "blair athol", "caol ila", "glen elgin", "glen garioch", "glen grant",
             "glen keith", "glen moray", "glen ord", "glen scotia", "glen spey", "highland park", "loch lomond",
             "north port", "port charlotte", "port ellen", "royal brackla", "royal lochnagar", "isle of harris",
             "buffalo trace", "eagle rare", "van winkle", "wild turkey", "elijah craig", "heaven hill", "four roses",
             "maker's mark", "woodford reserve", "jack daniel's", "old forester", "knob creek", "jim beam",
             "e.h. taylor", "old fitzgerald", "paul john", "green spot", "gordon & macphail", "douglas laing",
             "hunter laing", "elixir distillers", "that boutique-y", "decadent drinks", "whisky agency",
             "scotch malt whisky society", "berry bros", "malts of scotland", "wu dram clan", "thompson bros",
             "old particular", "first editions", "artful dodger", "port askaig", "an cnoc", "milk & honey", "secret speyside", "secret highland"]
    text = NAMES
    for m in multi:
        words.add(m)
        text = text.replace(m, " ")
    words |= {t for t in text.split() if len(t) >= 4}
    try:
        for group in json.load(open(bt.BRANDS_FILE)).get("brands", []):
            words |= {x.lower() for x in group if len(x) >= 3}
    except Exception:
        pass
    return words


_NAME_SET = None


def name_hit(title):
    global _NAME_SET
    if _NAME_SET is None:
        _NAME_SET = _names()
    t = title.lower()
    return any(n in t for n in _NAME_SET)


def is_whisky(e):
    """샵 분류값 → 제목 순으로 판정. 연식만 있는 건 약한 신호다(럼·브랜디에도 연식이 붙는다)."""
    title, hint = e.get("title", ""), (e.get("hint") or "")
    if sources._is_small(title) or MERCH_RX.search(title):
        return False
    ptype, _, tags = hint.partition("|")
    for h in (ptype, tags) if "|" in hint else (hint,):
        if not h.strip():
            continue
        if WHISKY_RX.search(h):
            return True
        if NON_RX.search(h):
            return False
    strong = bool(WHISKY_RX.search(title) or name_hit(title))
    if NON_RX.search(title):
        return strong            # 'Ardbeg ... rum cask'처럼 증류소·위스키 단어가 있으면 위스키
    return strong or bt.title_age(title) is not None


# ---------------------------------------------------------------- 리포트
def fmt_price(e, rates):
    p, cur = e.get("price"), e.get("cur")
    if p in (None, ""):
        return "가격 미확인"
    try:
        v = float(p)
    except ValueError:
        return str(p)
    s = f"{SYM.get(cur, '')}{v:,.0f}" if cur in ("JPY", "KRW") else f"{SYM.get(cur, '')}{v:,.2f}"
    krw = bt.to_krw(v, cur, rates) if cur and cur != "KRW" else None
    return s + (f" (≈{krw / 10000:,.1f}만원)" if krw else "")


def load_events():
    out = []
    try:
        with EVENTS.open() as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        out.append(json.loads(line))
                    except ValueError:
                        pass
    except FileNotFoundError:
        pass
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--hours", type=float, help="last_sent 대신 최근 N시간")
    ap.add_argument("--auto", action="store_true", help="발송 시각이 지났고 아직 안 보냈을 때만 발송")
    args = ap.parse_args()

    now = bt.now().replace(tzinfo=None)
    state = bt.load_json(STATE, {})
    if args.auto:
        if not state.get("last_sent"):            # 처음엔 조용히 시작점만 찍는다(엉뚱한 시각에 첫 리포트 방지)
            state["last_sent"] = now.isoformat(timespec="seconds")
            STATE.write_text(json.dumps(state, ensure_ascii=False))
            bt.log("[DIGEST] 시작점 기록 — 다음 08·19시부터 발송")
            return
        today = [now.replace(hour=h, minute=m, second=0, microsecond=0) for h, m in SLOTS]
        slots = [t for t in today if t <= now] or [today[-1] - timedelta(days=1)]
        if datetime.fromisoformat(state["last_sent"]) >= max(slots):
            return                                 # 이번 슬롯은 이미 보냄
    since = (now - timedelta(hours=args.hours)) if args.hours else (
        datetime.fromisoformat(state["last_sent"]) if state.get("last_sent") else now - timedelta(hours=12))
    events = load_events()
    window = [e for e in events if datetime.fromisoformat(e["ts"]) > since]

    # 같은 상품·같은 종류는 한 번만(가장 최근 값)
    uniq = OrderedDict()
    for e in window:
        uniq[(e["site"], e["id"], e["type"])] = e
    items = [e for e in uniq.values() if is_whisky(e)]
    dropped = len(uniq) - len(items)

    rates = bt.fx_rates({})
    wl = bt.load_json(bt.WATCHLIST, {})
    slot = "오전" if now.hour < 13 else "저녁"
    new = [e for e in items if e["type"] == "new"]
    rs = [e for e in items if e["type"] == "restock"]
    stars = [e for e in items if e.get("kw")]

    def line(e, html=True):
        tag = "🆕" if e["type"] == "new" else "🔁"
        oos = " · 품절" if e.get("available") is False else ""
        title = tg.esc(e["title"]) if html else e["title"]
        shop = SHOP_LABEL.get(e["site"], e["site"])
        return f"{tag} {title}\n   {shop} · {fmt_price(e, rates)}{oos}\n   {e['url']}"

    head = (f"🛍 샵 리포트 ({now.strftime('%m/%d')} {slot}) — 위스키 신제품 {len(new)} · 재입고 {len(rs)}"
            f"\n{since.strftime('%m/%d %H:%M')} 이후")
    # 텔레그램: ⭐ + 요약
    by_shop = OrderedDict()
    for e in sorted(items, key=lambda x: x["site"]):
        c = by_shop.setdefault(e["site"], [0, 0])
        c[0 if e["type"] == "new" else 1] += 1
    summary = "\n".join(f"· {SHOP_LABEL.get(s, s)}  신 {a} / 재 {b}" for s, (a, b) in by_shop.items()) or "· 변화 없음"
    tg_body = f"<b>{tg.esc(head)}</b>\n\n"
    if stars:
        tg_body += f"<b>⭐ 취향 매치 {len(stars)}건</b>\n" + "\n".join(line(e) for e in stars[:30])
        if len(stars) > 30:
            tg_body += f"\n…외 {len(stars) - 30}건(메일 참조)"
        tg_body += "\n\n"
    else:
        tg_body += "⭐ 취향 매치 없음\n\n"
    tg_body += "<b>샵별 요약</b>\n" + summary + ("\n\n전체 목록은 메일로 보냈습니다." if items else "")

    # 메일: 전체 목록(⭐ 먼저, 그다음 샵별 신제품 → 재입고)
    mail = [head, ""]
    if stars:
        mail += [f"⭐ 취향 매치 {len(stars)}건", ""] + [line(e, html=False) for e in stars] + [""]
    for s in by_shop:
        sub = [e for e in items if e["site"] == s]
        mail += [f"■ {SHOP_LABEL.get(s, s)} — 신제품 {sum(e['type'] == 'new' for e in sub)} · "
                 f"재입고 {sum(e['type'] == 'restock' for e in sub)}", ""]
        mail += [line(e, html=False) for e in sorted(sub, key=lambda x: (x["type"] != "new", x["title"]))] + [""]
    if not items:
        mail.append("이번 구간에는 위스키 신제품·재입고가 없습니다.")
    mail.append(f"(위스키 외 {dropped}건 제외 · 🆕 신제품 · 🔁 재입고)")

    if args.dry:
        print(re.sub(r"<[^>]+>", "", tg_body))
        print("\n" + "=" * 60 + "\n")
        print("\n".join(mail))
        return

    chat = bt.load_json(TRACKER_STATE, {}).get("chat_id")
    if chat:
        tg.send(chat, tg_body)
    to = (wl.get("notify") or {}).get("email")
    if to:
        whisky_watch.send_email(f"[위스키] 샵 리포트 {now.strftime('%m/%d')} {slot} — 신제품 {len(new)} · 재입고 {len(rs)}",
                                "\n".join(mail), to)
    bt.log(f"[DIGEST] 샵 리포트 발송 — 신제품 {len(new)} · 재입고 {len(rs)} · ⭐ {len(stars)} (위스키 외 {dropped} 제외)")

    state["last_sent"] = now.isoformat(timespec="seconds")
    STATE.write_text(json.dumps(state, ensure_ascii=False))
    cutoff = now - timedelta(days=7)                  # 이벤트 파일 정리
    keep = [e for e in events if datetime.fromisoformat(e["ts"]) > cutoff]
    EVENTS.write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in keep))


if __name__ == "__main__":
    main()
