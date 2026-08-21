#!/usr/bin/env bash
set -euo pipefail

: "${LOCKBOX_SECRET_ID:?LOCKBOX_SECRET_ID is required}"
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
  '{entries: [
    {key:"YANDEX_DISK_TOKEN", text_value:$disk},
    {key:"VK_ACCESS_TOKEN", text_value:$vk_token},
    {key:"VK_CALLBACK_SECRET", text_value:$vk_secret},
    {key:"VK_CONFIRMATION_STRING", text_value:$vk_confirmation},
    {key:"VK_GROUP_ID", text_value:$vk_group},
    {key:"TG_ADMIN_IDS", text_value:$tg_admins},
    {key:"VK_ADMIN_IDS", text_value:$vk_admins},
    {key:"PARTICIPANT_KEY_HMAC_SECRET", text_value:$participant},
    {key:"CF_TO_YANDEX_HMAC_SECRET", text_value:$ingress},
    {key:"YANDEX_TO_CF_EGRESS_HMAC_SECRET", text_value:$egress},
    {key:"YMQ_ACCESS_KEY_ID", text_value:$ymq_id},
    {key:"YMQ_SECRET_ACCESS_KEY", text_value:$ymq_secret}
  ]}' >"${payload_file}"

yc lockbox payload add-version "${LOCKBOX_SECRET_ID}" --payload-file "${payload_file}" >/dev/null
echo "A new Lockbox version was installed"
