# whisky-watch (cloud)

GitHub Actions cron이 위스키 온라인숍 5곳(RUDDER·Mukawa·DeinWhisky·Shinanoya·Vitalaus)을 폴링한다.
맥 상태와 무관하게 돌고, LLM·토큰 사용 없음.

## 두 가지 일
1. **키워드 감시** (`whisky_watch.py`) — `watchlist.json` 키워드에 걸리는 신규 매물·세일을
   `data/inbox.md`에 쌓고 Gmail·텔레그램으로 알림.
2. **보틀 추적** (`bottle_tracker.py`) — 노션 위시리스트에서 `추적` 체크된 행을 읽어
   사이트별 재고·가격을 추적. 재입고·품절·가격하락·목표가 진입을 텔레그램으로 알리고
   노션 행(추적 현황·재고·최근확인가·통화·확인시각)에 써준다. `data/price_history.csv`에 이력 적재.

## 노션 속성 (위스키 리스트 DB)
- `추적` 체크 → 추적 시작. `추적 키워드`: 콤마 구분 별칭(가타카나·독일어 포함 가능).
  비우면 상품명에서 자동 생성(용량·도수·year/old 같은 범용 단어 제거).
- `추적 링크`: 사이트별 확정 상품 URL(줄바꿈 구분). 없으면 폴링 결과에서 후보를 찾아 텔레그램으로 제안.
- `목표가KRW`: 이 값 이하로 내려오면 🎯 알림.

## 텔레그램 명령
`/start` 등록 · `/add 보틀명`(한/영/일 혼용 OK — 현재 매물에서 후보를 찾아 번호 버튼으로 회신, 누른 것만 노션에 생성·링크 확정; `brands.json`이 표기 변환표) · `/list` 현황 · `/link 보틀명일부 URL` 링크 확정 · `/stop 보틀명일부` 해제
봇 응답은 폴링 구조라 다음 실행 때(≤20분, KST 17시대 10분) 온다.

## 비밀 (Repository secrets)
`GMAIL_APP_PASSWORD` · `NOTION_TOKEN`(내부 통합, DB에 연결 필요) · `TELEGRAM_BOT_TOKEN`(BotFather)

## 스케줄
`.github/workflows/watch.yml` — UTC 기준. KST 16:40~18:30은 10분 간격(무카와 17시 업데이트 대비), 그 외 08:10/13:10/23:10.
수동 실행: `gh workflow run watch.yml` (`-f test_email=true`면 테스트 메일만).
