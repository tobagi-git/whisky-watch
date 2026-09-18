# whisky-watch (cloud)

위스키 온라인숍 5곳(RUDDER·Mukawa·DeinWhisky·Shinanoya·Vitalaus)을 GitHub Actions cron으로 폴링해
키워드에 맞는 신규 매물·세일을 Gmail로 보낸다. 맥 상태와 무관하게 돈다. LLM·토큰 사용 없음.

- 스케줄: `.github/workflows/watch.yml` (UTC 기준, KST 16:40~18:30은 10분 간격)
- 감시 키워드·사이트·수신 메일: `watchlist.json`
- 상태: `data/seen.json`(이미 본 상품), `data/inbox.md`(주간 브리핑용 인박스), `data/watch.log`
- 비밀: `GMAIL_APP_PASSWORD` (Repository secret)
