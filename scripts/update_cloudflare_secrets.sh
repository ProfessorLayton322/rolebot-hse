#!/usr/bin/env bash
set -euo pipefail

: "${TG_WEBHOOK_SECRET:?TG_WEBHOOK_SECRET is required}"
: "${TG_BOT_TOKEN:?TG_BOT_TOKEN is required}"
: "${CF_TO_YANDEX_HMAC_SECRET:?CF_TO_YANDEX_HMAC_SECRET is required}"
: "${YANDEX_TO_CF_EGRESS_HMAC_SECRET:?YANDEX_TO_CF_EGRESS_HMAC_SECRET is required}"
: "${YANDEX_DISK_TOKEN:?YANDEX_DISK_TOKEN is required}"
: "${VK_ACCESS_TOKEN:?VK_ACCESS_TOKEN is required}"
: "${VK_CALLBACK_SECRET:?VK_CALLBACK_SECRET is required}"
: "${VK_CONFIRMATION_STRING:?VK_CONFIRMATION_STRING is required}"
: "${VK_GROUP_ID:?VK_GROUP_ID is required}"
: "${TG_ADMIN_IDS:?TG_ADMIN_IDS is required}"
: "${VK_ADMIN_IDS:?VK_ADMIN_IDS is required}"
: "${TG_GAMEMASTER_IDS:?TG_GAMEMASTER_IDS is required}"
: "${VK_GAMEMASTER_IDS:?VK_GAMEMASTER_IDS is required}"
: "${PARTICIPANT_KEY_HMAC_SECRET:?PARTICIPANT_KEY_HMAC_SECRET is required}"
: "${YMQ_ACCESS_KEY_ID:?YMQ_ACCESS_KEY_ID is required}"
: "${YMQ_SECRET_ACCESS_KEY:?YMQ_SECRET_ACCESS_KEY is required}"
: "${YANDEX_OIDC_AUDIENCE:?YANDEX_OIDC_AUDIENCE is required}"
: "${YANDEX_SERVICE_ACCOUNT_IDS:?YANDEX_SERVICE_ACCOUNT_IDS is required}"
: "${YANDEX_GATEWAY_URL:?YANDEX_GATEWAY_URL is required}"
: "${INGRESS_WORKER_NAME:?INGRESS_WORKER_NAME is required}"
: "${EGRESS_WORKER_NAME:?EGRESS_WORKER_NAME is required}"

validate_numeric_ids() {
  local name="$1"
  local value="$2"
  if ! printf '%s' "${value}" | jq -e \
    'type == "array" and all(.[]; type == "number" and floor == . and . > 0)' >/dev/null; then
    echo "${name} must be a JSON array of positive numeric user IDs" >&2
    return 1
  fi
}

validate_numeric_ids "TG_ADMIN_IDS" "${TG_ADMIN_IDS}"
validate_numeric_ids "VK_ADMIN_IDS" "${VK_ADMIN_IDS}"
validate_numeric_ids "TG_GAMEMASTER_IDS" "${TG_GAMEMASTER_IDS}"
validate_numeric_ids "VK_GAMEMASTER_IDS" "${VK_GAMEMASTER_IDS}"
if ! printf '%s' "${YANDEX_SERVICE_ACCOUNT_IDS}" | jq -e \
  'type == "array" and length == 2 and all(.[]; type == "string" and length > 0)' >/dev/null; then
  echo "YANDEX_SERVICE_ACCOUNT_IDS must contain the two runtime service-account IDs" >&2
  exit 1
fi

jq -n \
  --arg webhook "${TG_WEBHOOK_SECRET}" \
  --arg hmac "${CF_TO_YANDEX_HMAC_SECRET}" \
  --arg gateway "${YANDEX_GATEWAY_URL}" \
  '{TG_WEBHOOK_SECRET:$webhook,CF_TO_YANDEX_HMAC_SECRET:$hmac,YANDEX_GATEWAY_URL:$gateway}' \
  | npx --yes wrangler@4.125.0 secret bulk \
      --config cloudflare/telegram-ingress/wrangler.toml \
      --name "${INGRESS_WORKER_NAME}"

jq -n \
  --arg token "${TG_BOT_TOKEN}" \
  --arg hmac "${YANDEX_TO_CF_EGRESS_HMAC_SECRET}" \
  --arg disk "${YANDEX_DISK_TOKEN}" \
  --arg vk_token "${VK_ACCESS_TOKEN}" \
  --arg vk_secret "${VK_CALLBACK_SECRET}" \
  --arg vk_confirmation "${VK_CONFIRMATION_STRING}" \
  --arg vk_group "${VK_GROUP_ID}" \
  --arg tg_admins "${TG_ADMIN_IDS}" \
  --arg vk_admins "${VK_ADMIN_IDS}" \
  --arg tg_gamemasters "${TG_GAMEMASTER_IDS}" \
  --arg vk_gamemasters "${VK_GAMEMASTER_IDS}" \
  --arg participant "${PARTICIPANT_KEY_HMAC_SECRET}" \
  --arg ingress "${CF_TO_YANDEX_HMAC_SECRET}" \
  --arg ymq_id "${YMQ_ACCESS_KEY_ID}" \
  --arg ymq_secret "${YMQ_SECRET_ACCESS_KEY}" \
  --arg audience "${YANDEX_OIDC_AUDIENCE}" \
  --arg subjects "${YANDEX_SERVICE_ACCOUNT_IDS}" \
  '{
    TG_BOT_TOKEN:$token,
    YANDEX_TO_CF_EGRESS_HMAC_SECRET:$hmac,
    YANDEX_DISK_TOKEN:$disk,
    VK_ACCESS_TOKEN:$vk_token,
    VK_CALLBACK_SECRET:$vk_secret,
    VK_CONFIRMATION_STRING:$vk_confirmation,
    VK_GROUP_ID:$vk_group,
    TG_ADMIN_IDS:$tg_admins,
    VK_ADMIN_IDS:$vk_admins,
    TG_GAMEMASTER_IDS:$tg_gamemasters,
    VK_GAMEMASTER_IDS:$vk_gamemasters,
    PARTICIPANT_KEY_HMAC_SECRET:$participant,
    CF_TO_YANDEX_HMAC_SECRET:$ingress,
    YMQ_ACCESS_KEY_ID:$ymq_id,
    YMQ_SECRET_ACCESS_KEY:$ymq_secret,
    YANDEX_OIDC_AUDIENCE:$audience,
    YANDEX_SERVICE_ACCOUNT_IDS:$subjects
  }' \
  | npx --yes wrangler@4.125.0 secret bulk \
      --config cloudflare/telegram-egress/wrangler.toml \
      --name "${EGRESS_WORKER_NAME}"
