# whisky-watch (cloud)

GitHub Actions cron이 위스키 온라인숍 5곳(RUDDER·Mukawa·DeinWhisky·Shinanoya·Vitalaus)을 폴링한다.
맥 상태와 무관하게 돌고, LLM·토큰 사용 없음.

## 두 가지 일
1. **키워드 감시** (`whisky_watch.py`) — `watchlist.json` 키워드에 걸리는 신규 매물·세일을
   `data/inbox.md`에 쌓고 Gmail·텔레그램으로 알림.
2. **보틀 추적** (`bottle_tracker.py`) — 노션 위시리스트에서 `추적` 체크된 행을 읽어
   사이트별 재고·가격을 추적. 재입고·품절·가격하락·목표가 진입을 텔레그램으로 알리고
   노션 행(추적 현황·재고·최근확인가·통화·확인시각)에 써준다. `data/price_history.csv`에 이력 적재.

3. **경매·영국숍 감시** (`auction_watch.py` + `sources.py`) — 원하는 보틀을 사이트 15곳에서 찾아 알린다.
   감시 대상은 `auction_watchlist.json`에 적은 항목 + **노션에서 `추적`=✓ 인 행 전부**(텔레그램 `/add`로
   넣은 보틀이 자동 포함). 알림: 🆕 신규 · ⏰ 종료 3시간 이내(야후) · 📉 리테일 가격하락 · 🎯 목표가 진입. **텔레그램 + 메일 둘 다** 발송.
   본문에 **일본 수령 기준 추정 총액**(사이트별 수수료·일본 배송비 반영)을 함께 준다. 상태는 `data/auction_seen.json`.

   | 사이트 | 구분 | 수집 방식 | 수수료·VAT·배송(일본·한국) |
   |---|---|---|---|
   | 야후옥션 🇯🇵 | 경매 | HTML data-* 속성 | 대행 5%, 국제배송 별도 |
   | Scotch Whisky Auctions 🇬🇧 | 경매 | HTML(POST 정렬) | 15% + 수수료VAT, 배송 개별견적(DHL) |
   | Just Whisky 🇬🇧 | 경매 | 공개 JSON API | 12.5%(영국 외 VAT 없음) + £69 |
   | Whisky.Auction 🇬🇧 | 경매 | HTML | 15% + £37 |
   | WIO 🇬🇧 | 리테일 | Shopify | VAT 20% 제외 + £33 |
   | The Whisky Barrel 🇬🇧 | 리테일 | Shopify | VAT 제외 + £44 |
   | Inn-Out 🇩🇪 | 리테일 | Shopify | 독일 VAT 19% 제외 + €49.99(아시아) |
   | Top Whiskies 🇬🇧 | 리테일 | Shopify | VAT 제외, 배송 결제 시 |
   | Abbey Whisky 🇬🇧 | 리테일 | Shopify | VAT 제외, DHL 결제 시 |
   | Really Good Whisky 🇬🇧 | 리테일 | Shopify | VAT 제외, 직배 여부 주문 전 확인 |
   | HTFW 🇬🇧 | 리테일 | HTML + GA4 데이터 | VAT 미확인, 배송 결제 시·무료보험 |
   | Whisky-Maniac 🇩🇪 | 리테일 | schema.org 마이크로데이터 | VAT 미확인, €40~ |
   | Nickolls & Perks 🇬🇧 | 리테일 | WooCommerce Store API | API 가격이 이미 VAT 제외가 |
   | Whiskysite.nl 🇳🇱 | 리테일 | Lightspeed ?format=json | **일본만** 배송, VAT 미확인 |
   | La Maison du Whisky 🇫🇷 | 리테일 | Magento GraphQL | 존5(한·일) €20~, VAT 미확인 |

   - 리테일 샵들은 키워드 신착 폴러(`whisky_watch.py`)에도 붙어 있다. **새 샵의 첫 수집은 기준선으로만 기록**한다
     (카탈로그 수천 개가 '신규'로 쏟아지는 것 방지). VAT 차감이 확인 안 된 샵은 추정가에 반영하지 않는다(🎯 오판 방지).
   - ⚠️ Shopify 가격은 `products.json`·`/products/{handle}.js`(샵 기준 통화)에서만 읽는다. `suggest.json`·쿠키 가격은
     Shopify Markets 때문에 접속 국가 통화로 바뀐다(같은 상품 £97.96 / ₩149,753). 러너는 미국 IP다.
   - 영국 사이트는 **일본 배송이 되는 곳**만 넣었다(일본 수령 후 휴대 반입이 세금상 최선).
     Whisky Hammer·Whisky Auctioneer는 일본 배송은 되지만 평문 요청을 403/404로 막아 미지원.
   - 한국까지 정식통관하면 위 추정액에 **약 2.55배**가 더 붙는다. 일본 측 주세·소비세는 수취인 부담.
   - 사이트별 함정은 `sources.py` 상단 주석 참고(야후 404=0건, SWA 정렬은 POST, JW는 `name` 파라미터만 먹음).

4. **샵 리포트** (`shop_digest.py`) — 매일 **08:00·19:00 KST** 감시 샵들의 **위스키 신제품·재입고**를 모아 보낸다(하루 정리용).
   **즉시 알림**(2026-09-26): 폴링마다 새로 잡힌 위스키 신제품·재입고를 ⭐ 키워드 신규·💰 가격 하락과 **한 메시지로 묶어**
   **텔레그램으로만** 바로 보낸다(`whisky_watch.send_instant`, 메일은 08·19시 리포트만). 폴링은 08~24시 30분 간격(16:40~18:30은 10분), 밤 2시간.
   신제품은 30분 안에 잡히고, 오래된 상품의 재입고는 07:55·18:55 정밀 스캔에서만 잡힌다(정밀 스캔 이벤트는 즉시 알림 대신 리포트로).
   관심 보틀 감시(3번)와는 별도 리포트이고, 즉시 키워드 알림(1번)은 그대로 둔다.
   - 텔레그램 = ⭐(watchlist 키워드 매치) 목록 + 샵별 건수, 메일 = 전체 목록(가격 원화 병기).
   - 재료는 폴러가 매 실행 `data/shop_events.jsonl`에 쌓는 이벤트(new / 품절→재고 restock).
     Worker가 07:55·18:55에 `deep=true`로 돌려 **카탈로그 전체**를 읽어 오래된 상품의 재입고까지 잡는다(약 3분).
     정밀 스캔에서 처음 보는 옛 상품은 신규로 치지 않는다. Shopify는 연달아 긁으면 503을 주므로 재시도·1초 간격.
   - `--auto`: 발송 시각(07:50·18:50 이후)이 지났고 미발송일 때만 보낸다. GitHub는 대기 중 실행을 새 실행이 오면
     취소하므로, 전용 실행이 밀려도 다음 실행이 대신 보낸다. 위스키 판별은 샵 분류값 → 제목(증류소·병입자명,
     피트·아일라 등 강한 신호 / 연식만은 약한 신호), 샘플·35cl 미만·굿즈·칵테일 제외.

## 노션 속성 (위스키 리스트 DB)
- `추적` 체크 → 추적 시작. `추적 키워드`: 콤마 구분 별칭(가타카나·독일어 포함 가능).
  비우면 상품명에서 자동 생성(용량·도수·year/old 같은 범용 단어 제거).
- `추적 링크`: 사이트별 확정 상품 URL(줄바꿈 구분). 없으면 폴링 결과에서 후보를 찾아 텔레그램으로 제안.
- `목표가KRW`: 이 값 이하로 내려오면 🎯 알림.

## 텔레그램 명령
`/start` 등록 · `/add 보틀명`(한/영/일 혼용 OK — 현재 매물에서 후보를 찾아 번호 버튼으로 회신, 누른 것만 노션에 생성·링크 확정; `brands.json`이 표기 변환표) · 위스키베이스 링크를 보내도 같은 흐름(슬러그로 검색, 확정 시 `WB_ID` 기록) · `/list` 현황 · `/link 보틀명일부 URL` 링크 확정 · `/stop 보틀명일부` 해제
봇 응답은 폴링 구조라 다음 실행 때 온다(보통 ≤1시간, KST 16:40~18:30은 ≤10분).

## 비밀 (Repository secrets)
`GMAIL_APP_PASSWORD` · `NOTION_TOKEN`(내부 통합, DB에 연결 필요) · `TELEGRAM_BOT_TOKEN`(BotFather)

## 스케줄
**주 트리거는 Cloudflare Worker** (`worker/`, 이름 `whisky-watch-trigger`). 매분 깨어나서
- KST 16:40~18:30은 10분 간격, 그 외엔 짝수시 10분에 이 워크플로를 dispatch (하루 23회)
- 텔레그램에 처리 안 된 메시지가 있으면 즉시 dispatch → 봇 응답 1~2분
GitHub 자체 예약(`watch.yml`의 cron)은 실측상 대부분 버려져서(하루 3~6회) 6시간 간격 예비로만 둔다.

Worker 시크릿: `GH_TOKEN`(fine-grained, whisky-watch 저장소 Actions 읽기/쓰기만, 만료 없음) · `TG_TOKEN`.
배포: `cd worker && npx wrangler deploy` · 로그: `npx wrangler tail`
수동 실행: `gh workflow run watch.yml` (`-f test_email=true`면 테스트 메일만).

- **Bruichladdich(옥토모어) 발매 시즌 감시:** `watchlist.json`의 `bruichladdich` 사이트는 `start: 2027-08-15`부터만 켜진다(`site_on()`) — 옥토모어 18시리즈(.1~.3 통상 9월 첫 주, .4 10~11월 온라인 한정) 발매 3주 전부터 신규 상품·재입고를 텔레그램으로 알린다. 시작일 전에는 건너뛴다.

## 국내 편의점·마트 픽업 감시 (2026-10-07 추가)

- `dailyshot_cvs`: 데일리샷 공개 목록 API(`api.dailyshot.co/items/?service_type=`)로 CU 오늘픽업(2)·CU 예약/CU Bar(7)·이마트24 오늘픽업(9)·이마트24 예약(10)을 매 실행 전부 읽는다(약 3,300개, 요청 35회 안팎). 품절 상품도 목록에 남아 있어 재입고(품절→판매중)를 잡는다. 상품 단위(전국) 목록이라 매장별 재고는 모른다.
- `lotteon`: 롯데ON 검색 결과에 박힌 상품 JSON을 검색어(`queries`)별로 읽고 주류(`alcoholYn=Y`)만 남긴다. 롯데마트 주류 스마트픽.
- 두 곳 모두 `kw_only: true` + 사이트 전용 `keywords`(한정·희소 보틀 위주) — 키워드에 걸린 상품의 신규·재입고만 텔레그램 즉시 알림으로 보내고, 상시품(라가불린 16 등)의 품절↔입고는 울리지 않는다. 보틀 추적기 후보 탐색(`latest_items.json`)에서도 뺀다.
- 결제는 하지 않는다(결제 PIN·성인인증 때문에 자동화 불가·비권장) — 알림 링크로 앱에서 직접 결제.
- 이마트(SSG·와인그랩)는 일반 요청을 403으로 막아 미지원. GS25 와인25플러스는 앱 전용이라 미지원.

## 카카오톡 '나에게 보내기' 사본 (2026-10-07 추가)

- `kakao.py`: 즉시 알림 중 `watchlist.json`의 `kakao.sites`(기본: `dailyshot_cvs`·`lotteon`) 건만 카카오톡 나와의 채팅으로 한 건씩(실행당 최대 5건) 보낸다. 텔레그램이 주 채널 — 나와의 채팅은 푸시가 안 올 수 있다.
- 최초 연결: Kakao Developers 앱 설정 후 로컬에서 `python3 kakao_setup.py` 1회. 키는 화면에 안 보이게 입력받아 `~/.config/whisky-watch/`에, 클라우드용 토큰은 `kakao_token.enc`(AES-256, 키는 GitHub Secret `KAKAO_TOKEN_KEY`)로 state 브랜치에 둔다(저장소가 공개라 암호화본만 올라간다).
- 리프레시 토큰(2개월)은 만료 1개월 전부터 자동 갱신돼 같은 파일에 다시 암호화 저장된다. 두 달 넘게 폴링이 멈추면 `kakao_setup.py` 재실행.
- 링크는 카카오 앱 [플랫폼 > Web > 사이트 도메인]에 등록한 도메인만 열린다(https://dailyshot.co, https://www.lotteon.com).

## 비탈라우스(Vita La) 수집 방식 변경 (2026-10-09)

2026-09 사이트 개편으로 옛 `/shop/list-XXXX` HTML 수집기가 조용히 0개를 냈다(308 리다이렉트 + 목록이 API로 채워짐). 지금은 사이트가 쓰는 공개 API(`/api/v1/products?categoryId=…&isActive=true&limit=100`)를 읽는다. 카테고리는 `watchlist.json`의 `sites.vitalaus.categories`(아메리칸·스카치·기타국가·주류특가, 상위 카테고리는 하위 포함). 가격은 USD, 상품 링크는 `/products/{SKU}`, 상품 ID는 `vitala:{product_id}`(`id_prefix`로 기준선 판정). 옛 `vitalaus:` 기록 60건은 무해하게 남아 있다. 새 상품 약 880개는 첫 실행에서 알림 없이 기준선으로만 기록된다.

## 세일 시작 메일 감지 + 수신 주소 시크릿화 (2026-10-10)

- 수신 메일 주소는 공개 저장소라 `watchlist.json`에 두지 않는다(자리표시자 '[수신 메일]'이 ascii 오류로 메일 발송을 깨뜨렸다). `NOTIFY_EMAIL` GitHub Secret(로컬은 `~/.config/whisky-watch/notify_email.txt`)에서 읽는다 — `whisky_watch.notify_email()`.
- `mail_watch.py`: Gmail IMAP(읽기 전용, 앱 비밀번호)에서 `mail_watch.senders` 발신자 도메인의 새 메일 **제목만** 보고 `keywords`(블랙프라이데이·사이버먼데이 등, 11/1~12/31)에 걸리면 텔레그램·카카오로 알린다. 본문은 가져오지 않고, 로그엔 건수만 남긴다(공개 로그). 첫 실행은 기준선만 기록, 30분 스로틀.
- 알림 후 `TWE 대조해줘` — Claude가 내장 브라우저로 `/specialoffers/whisky?pg=N`을 읽어 Notion 위시리스트와 대조한다(Cloudflare 때문에 클라우드 자동화 불가).
- 시험: Actions `test_mail`(스캔 건수만 로그), `test_email`(테스트 메일 1통 발송).
