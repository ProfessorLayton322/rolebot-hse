#!/usr/bin/env bash
set -euo pipefail

: "${LOCKBOX_SECRET_ID:?LOCKBOX_SECRET_ID is required}"
: "${YC_TOKEN:?YC_TOKEN is required}"
: "${YANDEX_DISK_TOKEN:?YANDEX_DISK_TOKEN is required}"
: "${VK_ACCESS_TOKEN:?VK_ACCESS_TOKEN is required}"
: "${VK_CALLBACK_SECRET:?VK_CALLBACK_SECRET is required}"
: "${VK_CONFIRMATION_STRING:?VK_CONFIRMATION_STRING is required}"
: "${VK_GROUP_ID:?VK_GROUP_ID is required}"
: "${TG_ADMIN_IDS:?TG_ADMIN_IDS is required}"
: "${VK_ADMIN_IDS:?VK_ADMIN_IDS is required}"
: "${PARTICIPANT_KEY_HMAC_SECRET:?PARTICIPANT_KEY_HMAC_SECRET is required}"
: "${CF_TO_YANDEX_HMAC_SECRET:?CF_TO_YANDEX_HMAC_SECRET is required}"
: "${YANDEX_TO_CF_EGRESS_HMAC_SECRET:?YANDEX_TO_CF_EGRESS_HMAC_SECRET is required}"
: "${YMQ_ACCESS_KEY_ID:?YMQ_ACCESS_KEY_ID is required}"
: "${YMQ_SECRET_ACCESS_KEY:?YMQ_SECRET_ACCESS_KEY is required}"

validate_admin_ids() {
  local variable_name="$1"
  local raw_value="$2"
  if ! printf '%s' "${raw_value}" | jq -e \
    'type == "array" and all(.[]; type == "number" and floor == . and . > 0)' \
    >/dev/null; then
    echo "${variable_name} must be a JSON array of positive numeric user IDs, for example [12345678]" >&2
    return 1
  fi
}

validate_admin_ids "TG_ADMIN_IDS" "${TG_ADMIN_IDS}"
validate_admin_ids "VK_ADMIN_IDS" "${VK_ADMIN_IDS}"

payload_file="$(mktemp)"
trap 'rm -f "${payload_file}"' EXIT
chmod 600 "${payload_file}"
jq -n \
  --arg disk "${YANDEX_DISK_TOKEN}" \
  --arg vk_token "${VK_ACCESS_TOKEN}" \
  --arg vk_secret "${VK_CALLBACK_SECRET}" \
  --arg vk_confirmation "${VK_CONFIRMATION_STRING}" \
  --arg vk_group "${VK_GROUP_ID}" \
  --arg tg_admins "${TG_ADMIN_IDS}" \
  --arg vk_admins "${VK_ADMIN_IDS}" \
  --arg participant "${PARTICIPANT_KEY_HMAC_SECRET}" \
  --arg ingress "${CF_TO_YANDEX_HMAC_SECRET}" \
  --arg egress "${YANDEX_TO_CF_EGRESS_HMAC_SECRET}" \
  --arg ymq_id "${YMQ_ACCESS_KEY_ID}" \
  --arg ymq_secret "${YMQ_SECRET_ACCESS_KEY}" \
  '{payloadEntries: [
    {key:"YANDEX_DISK_TOKEN", textValue:$disk},
    {key:"VK_ACCESS_TOKEN", textValue:$vk_token},
    {key:"VK_CALLBACK_SECRET", textValue:$vk_secret},
    {key:"VK_CONFIRMATION_STRING", textValue:$vk_confirmation},
    {key:"VK_GROUP_ID", textValue:$vk_group},
    {key:"TG_ADMIN_IDS", textValue:$tg_admins},
    {key:"VK_ADMIN_IDS", textValue:$vk_admins},
    {key:"PARTICIPANT_KEY_HMAC_SECRET", textValue:$participant},
    {key:"CF_TO_YANDEX_HMAC_SECRET", textValue:$ingress},
    {key:"YANDEX_TO_CF_EGRESS_HMAC_SECRET", textValue:$egress},
    {key:"YMQ_ACCESS_KEY_ID", textValue:$ymq_id},
    {key:"YMQ_SECRET_ACCESS_KEY", textValue:$ymq_secret}
  ]}' >"${payload_file}"

if ! operation_response="$(curl --fail-with-body --silent --show-error \
  --request POST \
  --header "Authorization: Bearer ${YC_TOKEN}" \
  --header 'Content-Type: application/json' \
  --data-binary "@${payload_file}" \
  "https://lockbox.api.cloud.yandex.net/lockbox/v1/secrets/${LOCKBOX_SECRET_ID}:addVersion")"; then
  echo "::error::Lockbox rejected the new runtime-secret version" >&2
  exit 1
fi

if ! operation_id="$(printf '%s' "${operation_response}" | jq -er \
  '.id | select(type == "string" and length > 0)' 2>/dev/null)"; then
  echo "::error::Lockbox returned no operation ID" >&2
  exit 1
fi

for ((attempt = 1; attempt <= 30; attempt++)); do
  if [[ "$(printf '%s' "${operation_response}" | jq -r '.done // false')" == "true" ]]; then
    if operation_error="$(printf '%s' "${operation_response}" | jq -er \
      '.error.message | select(type == "string" and length > 0)' 2>/dev/null)"; then
      operation_error="${operation_error//$'\n'/ }"
      echo "::error::Lockbox version operation failed: ${operation_error}" >&2
      exit 1
    fi
    echo "A new Lockbox version was installed"
    exit 0
  fi

  if ((attempt == 30)); then
    break
  fi
  sleep 2
  if ! operation_response="$(curl --fail-with-body --silent --show-error \
    --header "Authorization: Bearer ${YC_TOKEN}" \
    "https://operation.api.cloud.yandex.net/operations/${operation_id}")"; then
    echo "::error::Could not read Lockbox operation ${operation_id}" >&2
    exit 1
  fi
done

echo "::error::Timed out waiting for Lockbox operation ${operation_id}" >&2
exit 1
