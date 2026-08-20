#!/usr/bin/env bash
set -euo pipefail

: "${TG_BOT_TOKEN:?TG_BOT_TOKEN is required}"
: "${TG_WEBHOOK_SECRET:?TG_WEBHOOK_SECRET is required}"
: "${TELEGRAM_INGRESS_URL:?TELEGRAM_INGRESS_URL is required}"

response="$(curl --fail-with-body --silent --show-error \
  --request POST \
  "https://api.telegram.org/bot${TG_BOT_TOKEN}/setWebhook" \
  --data-urlencode "url=${TELEGRAM_INGRESS_URL}" \
  --data-urlencode "secret_token=${TG_WEBHOOK_SECRET}" \
  --data-urlencode 'allowed_updates=["message","callback_query"]' \
  --data-urlencode 'drop_pending_updates=false')"

python3 -c 'import json,sys; data=json.load(sys.stdin); assert data.get("ok") is True, data; print("Telegram webhook configured")' <<<"${response}"

