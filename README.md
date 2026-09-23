# whisky-watch (cloud)

GitHub Actions cron이 위스키 온라인숍 5곳(RUDDER·Mukawa·DeinWhisky·Shinanoya·Vitalaus)을 폴링한다.
맥 상태와 무관하게 돌고, LLM·토큰 사용 없음.

## 두 가지 일
1. **키워드 감시** (`whisky_watch.py`) — `watchlist.json` 키워드에 걸리는 신규 매물·세일을
   `data/inbox.md`에 쌓고 Gmail·텔레그램으로 알림.
2. **보틀 추적** (`bottle_tracker.py`) — 노션 위시리스트에서 `추적` 체크된 행을 읽어
   사이트별 재고·가격을 추적. 재입고·품절·가격하락·목표가 진입을 텔레그램으로 알리고
   노션 행(추적 현황·재고·최근확인가·통화·확인시각)에 써준다. `data/price_history.csv`에 이력 적재.

3. **경매·영국숍 감시** (`auction_watch.py` + `sources.py`) — 원하는 보틀을 사이트 5곳에서 찾아 알린다.
   감시 대상은 `auction_watchlist.json`에 적은 항목 + **노션에서 `추적`=✓ 인 행 전부**(텔레그램 `/add`로
   넣은 보틀이 자동 포함). 알림: 🆕 신규 · ⏰ 종료 3시간 이내(야후) · 📉 리테일 가격하락 · 🎯 목표가 진입.
   본문에 **일본 수령 기준 추정 총액**(사이트별 수수료·일본 배송비 반영)을 함께 준다. 상태는 `data/auction_seen.json`.

   | 사이트 | 구분 | 수수료·배송(일본) |
   |---|---|---|
   | 야후옥션 🇯🇵 | 경매 | 대행 5%, 국제배송 별도 |
   | Scotch Whisky Auctions 🇬🇧 | 경매 | 15% + 수수료VAT, 배송 개별견적(DHL) |
   | Just Whisky 🇬🇧 | 경매 | 12.5%(영국 외 VAT 없음) + £69 |
   | Whisky.Auction 🇬🇧 | 경매 | 15% + £37 |
   | WIO 🇬🇧 | 리테일 | **VAT 20% 차감** + £33 |

   - 영국 사이트는 **일본 배송이 되는 곳**만 넣었다(일본 수령 후 휴대 반입이 세금상 최선).
     Whisky Hammer·Whisky Auctioneer는 일본 배송은 되지만 평문 요청을 403/404로 막아 미지원.
   - 한국까지 정식통관하면 위 추정액에 **약 2.55배**가 더 붙는다. 일본 측 주세·소비세는 수취인 부담.
   - 사이트별 함정은 `sources.py` 상단 주석 참고(야후 404=0건, SWA 정렬은 POST, JW는 `name` 파라미터만 먹음).

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
