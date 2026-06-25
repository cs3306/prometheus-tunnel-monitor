#!/bin/sh
_ip=$(echo "$SSH_CLIENT" | awk '{print $1}')
_port=$(echo "$SSH_CLIENT" | awk '{print $3}')
curl -s -X POST http://127.0.0.1:36605/login-notify \
  -H "Content-Type: application/json" \
  -d "{
    \"user\": \"$(id -un)\",
    \"host\": \"$(uname -n)\",
    \"ip\": \"$_ip\",
    \"port\": \"$_port\",
    \"time\": \"$(date '+%Y-%m-%d %H:%M:%S UTC' -u)\"
  }" &
