#!/bin/zsh
# 야후옥션 신규 매물 알림 — Mac 로컬 실행(launchd가 5분마다 호출). GitHub Actions를 쓰지 않는다.
# 시크릿은 ~/.config/whisky-watch/ 에서 읽고 이 저장소에는 두지 않는다(저장소가 공개).
# 사용: run_yahoo_alert.sh [auction_watch.py 인자…]   예) --seed (첫 실행: 알림 없이 현재 매물만 기록)
set -u
HERE="${0:A:h}/.."
CONF="$HOME/.config/whisky-watch"
export TELEGRAM_BOT_TOKEN="$(cat "$CONF/telegram_token.txt" 2>/dev/null)"
export TELEGRAM_CHAT_ID="$(cat "$CONF/telegram_chat_id.txt" 2>/dev/null)"
export NOTION_TOKEN="$(cat "$CONF/notion_token.txt" 2>/dev/null)"
if [[ "$*" != *"--seed"* && "$*" != *"--dry"* && ( -z "$TELEGRAM_BOT_TOKEN" || -z "$TELEGRAM_CHAT_ID" ) ]]; then
  echo "$(date '+%F %T') 텔레그램 토큰/채팅ID 없음 — 실행하지 않음(알림이 '본 것'으로 처리되는 것을 막기 위해)"; exit 0
fi
export WHISKY_DATA_DIR="$HOME/Claude/Projects/위스키/모니터링/yahoo_local"
mkdir -p "$WHISKY_DATA_DIR"
cd "$HERE" || exit 1
exec /opt/homebrew/bin/python3 auction_watch.py --only-source yahoo "$@"
