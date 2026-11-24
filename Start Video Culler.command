#!/bin/zsh
cd "$(dirname "$0")"
LOG_FILE=$(mktemp)
python3 culler.py --port 8788 > "$LOG_FILE" 2>&1 &
SERVER_PID=$!
for _ in {1..20}; do
  URL=$(grep -Eo 'http://127\.0\.0\.1:[0-9]+' "$LOG_FILE" | head -n 1)
  if [[ -n "$URL" ]]; then
    open "$URL"
    break
  fi
  sleep 0.2
done
cat "$LOG_FILE"
wait $SERVER_PID

