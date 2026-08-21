#!/usr/bin/env bash
set -euo pipefail

: "${TG_WEBHOOK_SECRET:?TG_WEBHOOK_SECRET is required}"
: "${TG_BOT_TOKEN:?TG_BOT_TOKEN is required}"
: "${CF_TO_YANDEX_HMAC_SECRET:?CF_TO_YANDEX_HMAC_SECRET is required}"
: "${YANDEX_TO_CF_EGRESS_HMAC_SECRET:?YANDEX_TO_CF_EGRESS_HMAC_SECRET is required}"
: "${YANDEX_GATEWAY_URL:?YANDEX_GATEWAY_URL is required}"
: "${INGRESS_WORKER_NAME:?INGRESS_WORKER_NAME is required}"
: "${EGRESS_WORKER_NAME:?EGRESS_WORKER_NAME is required}"

jq -n \
  --arg webhook "${TG_WEBHOOK_SECRET}" \
  --arg hmac "${CF_TO_YANDEX_HMAC_SECRET}" \
  --arg gateway "${YANDEX_GATEWAY_URL}" \
  '{TG_WEBHOOK_SECRET:$webhook,CF_TO_YANDEX_HMAC_SECRET:$hmac,YANDEX_GATEWAY_URL:$gateway}' \
  | npx --yes wrangler@4.125.0 secret bulk --name "${INGRESS_WORKER_NAME}"

jq -n \
  --arg token "${TG_BOT_TOKEN}" \
  --arg hmac "${YANDEX_TO_CF_EGRESS_HMAC_SECRET}" \
  '{TG_BOT_TOKEN:$token,YANDEX_TO_CF_EGRESS_HMAC_SECRET:$hmac}' \
  | npx --yes wrangler@4.125.0 secret bulk --name "${EGRESS_WORKER_NAME}"
