#!/usr/bin/env bash
set -euo pipefail

required=(
  TG_WEBHOOK_SECRET
  TELEGRAM_INGRESS_URL
  VK_CALLBACK_URL
  VK_CALLBACK_SECRET
  VK_CONFIRMATION_STRING
  VK_GROUP_ID
)
for name in "${required[@]}"; do
  if test -z "${!name:-}"; then
    echo "::error::${name} is required" >&2
    exit 1
  fi
done

response_file="$(mktemp)"
trap 'rm -f "${response_file}"' EXIT

telegram_body='{"update_id":-1,"message":{"message_id":-1,"from":{"id":1},"chat":{"id":1,"type":"private"},"text":"maintenance-probe"}}'
status="$(curl --silent --show-error --output "${response_file}" --write-out '%{http_code}' \
  --request POST \
  --header 'Content-Type: application/json' \
  --header "X-Telegram-Bot-Api-Secret-Token: ${TG_WEBHOOK_SECRET}" \
  --data "${telegram_body}" \
  "${TELEGRAM_INGRESS_URL}")"
test "${status}" = "200"
test ! -s "${response_file}"

vk_confirmation_body="$(python3 -c '
import json
import sys
print(json.dumps({"type": "confirmation", "group_id": int(sys.argv[1]), "secret": sys.argv[2]}))
' "${VK_GROUP_ID}" "${VK_CALLBACK_SECRET}")"
status="$(curl --silent --show-error --output "${response_file}" --write-out '%{http_code}' \
  --request POST \
  --header 'Content-Type: application/json' \
  --data "${vk_confirmation_body}" \
  "${VK_CALLBACK_URL}")"
test "${status}" = "200"
test "$(cat "${response_file}")" = "${VK_CONFIRMATION_STRING}"

vk_message_body="$(python3 -c '
import json
import sys
print(json.dumps({
    "type": "message_new",
    "event_id": "maintenance-probe",
    "group_id": int(sys.argv[1]),
    "secret": sys.argv[2],
    "object": {"message": {"from_id": 1, "peer_id": 1, "text": "maintenance-probe"}},
}, separators=(",", ":")))
' "${VK_GROUP_ID}" "${VK_CALLBACK_SECRET}")"
status="$(curl --silent --show-error --output "${response_file}" --write-out '%{http_code}' \
  --request POST \
  --header 'Content-Type: application/json' \
  --data "${vk_message_body}" \
  "${VK_CALLBACK_URL}")"
test "${status}" = "200"
test "$(cat "${response_file}")" = "ok"

echo "Cloudflare maintenance routing smoke test passed"
