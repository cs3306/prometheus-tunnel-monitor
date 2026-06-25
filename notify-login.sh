#!/bin/bash
[ "$PAM_TYPE" = "open_session" ] || exit 0
_port=$(echo "$SSH_CONNECTION" | awk '{print $4}')
curl -s -X POST http://127.0.0.1:36605/login-notify \
  -H "Content-Type: application/json" \
  -d "{
    \"user\": \"$PAM_USER\",
    \"host\": \"$(hostname)\",
    \"ip\": \"$PAM_RHOST\",
    \"port\": \"${_port:-unknown}\",
    \"time\": \"$(date '+%Y-%m-%d %H:%M:%S UTC' -u)\"
  }" &
