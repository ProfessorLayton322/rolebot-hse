#!/usr/bin/env bash
set -euo pipefail

required=(
  TG_BOT_TOKEN
  TG_WEBHOOK_SECRET
  TG_ADMIN_IDS
  TELEGRAM_INGRESS_URL
  TELEGRAM_EGRESS_URL
  YANDEX_GATEWAY_URL
  CF_TO_YANDEX_HMAC_SECRET
  YANDEX_TO_CF_EGRESS_HMAC_SECRET
  VK_CALLBACK_URL
  VK_CALLBACK_SECRET
  VK_CONFIRMATION_STRING
  VK_GROUP_ID
  VK_ACCESS_TOKEN
  VK_ADMIN_IDS
)
for name in "${required[@]}"; do
  if test -z "${!name:-}"; then
    echo "::error::${name} is required" >&2
    exit 1
  fi
done

response_file="$(mktemp)"
trap 'rm -f "${response_file}"' EXIT

first_admin_id() {
  python3 -c '
import json
import sys

value = json.loads(sys.argv[1])
if not isinstance(value, list) or not value or any(type(item) is not int for item in value):
    raise SystemExit("admin IDs must be a non-empty JSON numeric array")
print(value[0])
' "$1"
}

unexpected_response() {
  local service="$1"
  local status="$2"
  local body
  body="$(head --bytes 1000 "${response_file}")"
  echo "::error::${service} returned unexpected HTTP ${status}: ${body}" >&2
  exit 1
}

tg_admin_id="$(first_admin_id "${TG_ADMIN_IDS}")"
vk_admin_id="$(first_admin_id "${VK_ADMIN_IDS}")"

# The token, registered URL and public ingress route must all be live. A disabled
# workers.dev route previously returned Cloudflare 1042 before the Worker ran.
status="$(curl --silent --show-error --output "${response_file}" --write-out '%{http_code}' \
  "https://api.telegram.org/bot${TG_BOT_TOKEN}/getWebhookInfo")"
test "${status}" = "200" || unexpected_response "Telegram getWebhookInfo" "${status}"
python3 - "${response_file}" "${TELEGRAM_INGRESS_URL}" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as stream:
    payload = json.load(stream)
assert payload.get("ok") is True, payload
actual = payload.get("result", {}).get("url")
assert actual == sys.argv[2], f"Telegram webhook is {actual!r}, expected {sys.argv[2]!r}"
PY

status="$(curl --silent --show-error --output "${response_file}" --write-out '%{http_code}' \
  --request POST \
  --header 'Content-Type: application/json' \
  --data '{}' \
  "${TELEGRAM_INGRESS_URL}")"
test "${status}" = "403" || unexpected_response "Telegram ingress authentication probe" "${status}"

# IAM binding and Function-version propagation can briefly outlive Terraform
# apply. An unsigned direct request reaches runtime configuration before HMAC
# validation, so HTTP 403 proves that a cold gateway instance can already mint
# an ID token and read its Worker bindings. Keep the real signed probe fresh by
# waiting here instead of retrying it with an aging timestamp and deadline.
gateway_ready=false
for attempt in $(seq 1 24); do
  status="$(curl --silent --show-error --output "${response_file}" --write-out '%{http_code}' \
    --request POST \
    --header 'Content-Type: application/json' \
    --data '{}' \
    "${YANDEX_GATEWAY_URL}")"
  if test "${status}" = "403"; then
    gateway_ready=true
    break
  fi
  if test "${attempt}" -lt 24; then
    sleep 5
  fi
done
test "${gateway_ready}" = true || unexpected_response "Telegram gateway readiness probe" "${status}"

# A signed direct Telegram update proves that the gateway can connect to YDB and
# that the configured Telegram administrator receives the admin menu.
tg_update_id="$(date +%s)${RANDOM}"
tg_body="$(python3 -c '
import json
import sys

update_id, admin_id = map(int, sys.argv[1:])
print(json.dumps({
    "update_id": update_id,
    "message": {
        "message_id": update_id,
        "from": {"id": admin_id},
        "chat": {"id": admin_id, "type": "private"},
        "text": "/start",
    },
}, separators=(",", ":")))
' "${tg_update_id}" "${tg_admin_id}")"
tg_request_id="smoke-tg-${GITHUB_RUN_ID:-manual}-${RANDOM}"
tg_timestamp="$(date +%s)"
tg_path="$(python3 -c 'from urllib.parse import urlparse; import sys; print(urlparse(sys.argv[1]).path)' "${YANDEX_GATEWAY_URL}")"
tg_signature="$(SIGNING_SECRET="${CF_TO_YANDEX_HMAC_SECRET}" python3 -c '
import hashlib
import hmac
import os
import sys

timestamp, request_id, path, body = sys.argv[1:]
body_hash = hashlib.sha256(body.encode()).hexdigest()
canonical = "\n".join((timestamp, request_id, "POST", path, body_hash))
print(hmac.new(os.environ["SIGNING_SECRET"].encode(), canonical.encode(), hashlib.sha256).hexdigest())
' "${tg_timestamp}" "${tg_request_id}" "${tg_path}" "${tg_body}")"
status="$(curl --silent --show-error --output "${response_file}" --write-out '%{http_code}' \
  --request POST \
  --header 'Content-Type: application/json' \
  --header "X-Gateway-Request-Id: ${tg_request_id}" \
  --header "X-Gateway-Timestamp: ${tg_timestamp}" \
  --header "X-Gateway-Signature: ${tg_signature}" \
  --header "X-Telegram-Inline-Deadline-Ms: $(( $(date +%s) * 1000 + 60000 ))" \
  --data "${tg_body}" \
  "${YANDEX_GATEWAY_URL}")"
test "${status}" = "200" || unexpected_response "Telegram gateway application probe" "${status}"
python3 - "${response_file}" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as stream:
    contract = json.load(stream)
assert contract.get("delivery") == "inline", contract
telegram = contract.get("telegram", {})
assert telegram.get("method") == "sendMessage", telegram
buttons = telegram.get("reply_markup", {}).get("inline_keyboard", [])
labels = [button.get("text") for row in buttons for button in row]
assert "🛠 Администрирование" in labels, labels
PY

# The real ingress must accept the configured webhook secret and complete the
# Cloudflare -> Yandex signed bridge. This update is distinct from the direct one.
tg_ingress_update_id="$((tg_update_id + 1))"
tg_ingress_body="$(python3 -c '
import json
import sys

update_id, admin_id = map(int, sys.argv[1:])
print(json.dumps({
    "update_id": update_id,
    "message": {
        "message_id": update_id,
        "from": {"id": admin_id},
        "chat": {"id": admin_id, "type": "private"},
        "text": "/start",
    },
}, separators=(",", ":")))
' "${tg_ingress_update_id}" "${tg_admin_id}")"
status="$(curl --silent --show-error --output "${response_file}" --write-out '%{http_code}' \
  --request POST \
  --header 'Content-Type: application/json' \
  --header "X-Telegram-Bot-Api-Secret-Token: ${TG_WEBHOOK_SECRET}" \
  --data "${tg_ingress_body}" \
  "${TELEGRAM_INGRESS_URL}")"
test "${status}" = "200" || unexpected_response "Telegram ingress live update" "${status}"

# Exercise the egress Worker and Telegram Bot API independently. This also
# confirms that the configured Telegram admin chat is reachable by the bot.
egress_request_id="smoke-egress-${GITHUB_RUN_ID:-manual}-${RANDOM}"
egress_timestamp="$(date +%s)"
egress_body="$(python3 -c '
import json
import sys

request_id, admin_id = sys.argv[1], int(sys.argv[2])
print(json.dumps({
    "request_id": request_id,
    "method": "sendMessage",
    "payload": {"chat_id": admin_id, "text": "✅ Проверка развёртывания Telegram-бота прошла успешно."},
}, ensure_ascii=False, separators=(",", ":")))
' "${egress_request_id}" "${tg_admin_id}")"
egress_signature="$(SIGNING_SECRET="${YANDEX_TO_CF_EGRESS_HMAC_SECRET}" python3 -c '
import hashlib
import hmac
import os
import sys

timestamp, request_id, body = sys.argv[1:]
body_hash = hashlib.sha256(body.encode()).hexdigest()
canonical = "\n".join((timestamp, request_id, "POST", "/telegram/send", body_hash))
print(hmac.new(os.environ["SIGNING_SECRET"].encode(), canonical.encode(), hashlib.sha256).hexdigest())
' "${egress_timestamp}" "${egress_request_id}" "${egress_body}")"
status="$(curl --silent --show-error --output "${response_file}" --write-out '%{http_code}' \
  --request POST \
  --header 'Content-Type: application/json' \
  --header "X-Timestamp: ${egress_timestamp}" \
  --header "X-Request-Id: ${egress_request_id}" \
  --header "X-Signature: ${egress_signature}" \
  --data "${egress_body}" \
  "${TELEGRAM_EGRESS_URL%/}/telegram/send")"
test "${status}" = "200" || unexpected_response "Telegram egress live send" "${status}"
python3 - "${response_file}" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as stream:
    payload = json.load(stream)
assert payload.get("ok") is True, payload
PY

# VK authentication, confirmation and real delivered admin messages are all
# separate checks so the smoke test catches callback drift, application stalls,
# and VK API failures after the callback has been accepted.
status="$(curl --silent --show-error --output "${response_file}" --write-out '%{http_code}' \
  --request POST \
  --header 'Content-Type: application/json' \
  --data '{}' \
  "${VK_CALLBACK_URL}")"
test "${status}" = "403" || unexpected_response "VK authentication probe" "${status}"

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
test "${status}" = "200" || unexpected_response "VK confirmation probe" "${status}"
test "$(cat "${response_file}")" = "${VK_CONFIRMATION_STRING}" || unexpected_response "VK confirmation probe" "${status}"

vk_latest_message_id() {
  local history_status
  history_status="$(curl --silent --show-error --output "${response_file}" --write-out '%{http_code}' \
    --request POST \
    --data-urlencode "access_token=${VK_ACCESS_TOKEN}" \
    --data-urlencode 'v=5.199' \
    --data-urlencode "peer_id=${vk_admin_id}" \
    --data-urlencode 'count=20' \
    'https://api.vk.com/method/messages.getHistory')"
  test "${history_status}" = "200" || unexpected_response "VK message history" "${history_status}"
  python3 - "${response_file}" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as stream:
    payload = json.load(stream)
assert "error" not in payload, payload["error"]
items = payload.get("response", {}).get("items", [])
print(max((item.get("id", 0) for item in items), default=0))
PY
}

vk_assert_outgoing_after() {
  local previous_id="$1"
  local expected_text="$2"
  local attempt history_status
  for attempt in 1 2 3 4 5; do
    history_status="$(curl --silent --show-error --output "${response_file}" --write-out '%{http_code}' \
      --request POST \
      --data-urlencode "access_token=${VK_ACCESS_TOKEN}" \
      --data-urlencode 'v=5.199' \
      --data-urlencode "peer_id=${vk_admin_id}" \
      --data-urlencode 'count=20' \
      'https://api.vk.com/method/messages.getHistory')"
    test "${history_status}" = "200" || unexpected_response "VK message history" "${history_status}"
    if python3 - "${response_file}" "${previous_id}" "${VK_GROUP_ID}" "${expected_text}" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as stream:
    payload = json.load(stream)
assert "error" not in payload, payload["error"]
previous_id, group_id = map(int, sys.argv[2:4])
expected = sys.argv[4]
items = payload.get("response", {}).get("items", [])
matches = [
    item
    for item in items
    if item.get("id", 0) > previous_id
    and item.get("out") == 1
    and item.get("from_id") == -group_id
    and expected in item.get("text", "")
]
raise SystemExit(0 if matches else 1)
PY
    then
      return 0
    fi
    sleep 1
  done
  echo "::error::VK did not deliver the expected bot response to the configured administrator" >&2
  exit 1
}

vk_message_body() {
  python3 -c '
import json
import sys

event_id, group_id, secret, admin_id, text = sys.argv[1], int(sys.argv[2]), sys.argv[3], int(sys.argv[4]), sys.argv[5]
print(json.dumps({
    "type": "message_new",
    "event_id": event_id,
    "group_id": group_id,
    "secret": secret,
    "object": {"message": {"from_id": admin_id, "peer_id": admin_id, "text": text}},
}, ensure_ascii=False, separators=(",", ":")))
' "$1" "${VK_GROUP_ID}" "${VK_CALLBACK_SECRET}" "${vk_admin_id}" "$2"
}

vk_before_start="$(vk_latest_message_id)"
vk_event_id="smoke-vk-start-${GITHUB_RUN_ID:-manual}-${RANDOM}"
vk_body="$(vk_message_body "${vk_event_id}" "/start")"
status="$(curl --silent --show-error --output "${response_file}" --write-out '%{http_code}' \
  --request POST \
  --header 'Content-Type: application/json' \
  --data "${vk_body}" \
  "${VK_CALLBACK_URL}")"
test "${status}" = "200" || unexpected_response "VK live /start update" "${status}"
test "$(cat "${response_file}")" = "ok" || unexpected_response "VK live /start update" "${status}"
vk_assert_outgoing_after "${vk_before_start}" "Привет! Этот бот поможет зарегистрироваться"

# Selecting the admin entry as plain text works both with VK keyboards enabled
# and with the API error-912 fallback, and proves that VK_ADMIN_IDS is active.
vk_before_admin="$(vk_latest_message_id)"
vk_event_id="smoke-vk-admin-${GITHUB_RUN_ID:-manual}-${RANDOM}"
vk_body="$(vk_message_body "${vk_event_id}" "🛠 Администрирование")"
status="$(curl --silent --show-error --output "${response_file}" --write-out '%{http_code}' \
  --request POST \
  --header 'Content-Type: application/json' \
  --data "${vk_body}" \
  "${VK_CALLBACK_URL}")"
test "${status}" = "200" || unexpected_response "VK live admin authorization" "${status}"
test "$(cat "${response_file}")" = "ok" || unexpected_response "VK live admin authorization" "${status}"
vk_assert_outgoing_after "${vk_before_admin}" "🛠 Администрирование"

echo "Telegram and VK live smoke tests passed"
