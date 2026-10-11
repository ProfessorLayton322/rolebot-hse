#!/usr/bin/env bash
set -euo pipefail

: "${ACTIVE_BACKEND:?ACTIVE_BACKEND is required}"
: "${TG_WEBHOOK_SECRET:?TG_WEBHOOK_SECRET is required}"
: "${TG_BOT_TOKEN:?TG_BOT_TOKEN is required}"
: "${CF_TO_YANDEX_HMAC_SECRET:?CF_TO_YANDEX_HMAC_SECRET is required}"
: "${CF_TO_AWS_HMAC_SECRET:?CF_TO_AWS_HMAC_SECRET is required}"
: "${YANDEX_TO_CF_EGRESS_HMAC_SECRET:?YANDEX_TO_CF_EGRESS_HMAC_SECRET is required}"
: "${AWS_TO_CF_EGRESS_HMAC_SECRET:?AWS_TO_CF_EGRESS_HMAC_SECRET is required}"
: "${YANDEX_DISK_TOKEN:?YANDEX_DISK_TOKEN is required}"
: "${GOOGLE_SERVICE_ACCOUNT_JSON:?GOOGLE_SERVICE_ACCOUNT_JSON is required}"
: "${VK_ACCESS_TOKEN:?VK_ACCESS_TOKEN is required}"
: "${VK_CALLBACK_SECRET:?VK_CALLBACK_SECRET is required}"
: "${VK_CONFIRMATION_STRING:?VK_CONFIRMATION_STRING is required}"
: "${VK_GROUP_ID:?VK_GROUP_ID is required}"
: "${TG_ADMIN_IDS:?TG_ADMIN_IDS is required}"
: "${VK_ADMIN_IDS:?VK_ADMIN_IDS is required}"
: "${TG_GAMEMASTER_IDS:?TG_GAMEMASTER_IDS is required}"
: "${VK_GAMEMASTER_IDS:?VK_GAMEMASTER_IDS is required}"
: "${PARTICIPANT_KEY_HMAC_SECRET:?PARTICIPANT_KEY_HMAC_SECRET is required}"
: "${INGRESS_WORKER_NAME:?INGRESS_WORKER_NAME is required}"
: "${EGRESS_WORKER_NAME:?EGRESS_WORKER_NAME is required}"

if [[ "${ACTIVE_BACKEND}" != "aws" ]]; then
  : "${YMQ_ACCESS_KEY_ID:?YMQ_ACCESS_KEY_ID is required outside AWS mode}"
  : "${YMQ_SECRET_ACCESS_KEY:?YMQ_SECRET_ACCESS_KEY is required outside AWS mode}"
  : "${YANDEX_OIDC_AUDIENCE:?YANDEX_OIDC_AUDIENCE is required outside AWS mode}"
  : "${YANDEX_SERVICE_ACCOUNT_IDS:?YANDEX_SERVICE_ACCOUNT_IDS is required outside AWS mode}"
fi

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
if [[ "${ACTIVE_BACKEND}" != "aws" ]]; then
  if ! printf '%s' "${YANDEX_SERVICE_ACCOUNT_IDS}" | jq -e \
    'type == "array" and length == 3 and all(.[]; type == "string" and length > 0)' >/dev/null; then
    echo "YANDEX_SERVICE_ACCOUNT_IDS must contain the three runtime service-account IDs" >&2
    exit 1
  fi
fi

telegram_bot_username="$(curl --fail-with-body --silent --show-error \
  "https://api.telegram.org/bot${TG_BOT_TOKEN}/getMe" \
  | jq -er '.result.username | select(type == "string" and test("^[A-Za-z0-9_]{5,32}$"))')"

jq -n \
  --arg webhook "${TG_WEBHOOK_SECRET}" \
  --arg vk_secret "${VK_CALLBACK_SECRET}" \
  --arg vk_confirmation "${VK_CONFIRMATION_STRING}" \
  --arg vk_group "${VK_GROUP_ID}" \
  --arg yandex_hmac "${CF_TO_YANDEX_HMAC_SECRET}" \
  --arg aws_hmac "${CF_TO_AWS_HMAC_SECRET}" \
  '{
    TG_WEBHOOK_SECRET:$webhook,
    VK_CALLBACK_SECRET:$vk_secret,
    VK_CONFIRMATION_STRING:$vk_confirmation,
    VK_GROUP_ID:$vk_group,
    CF_TO_YANDEX_HMAC_SECRET:$yandex_hmac,
    CF_TO_AWS_HMAC_SECRET:$aws_hmac
  }' \
  | npx --yes wrangler@4.125.0 secret bulk \
      --config cloudflare/telegram-ingress/wrangler.toml \
      --name "${INGRESS_WORKER_NAME}"

jq -n \
  --arg active_backend "${ACTIVE_BACKEND}" \
  --arg token "${TG_BOT_TOKEN}" \
  --arg telegram_username "${telegram_bot_username}" \
  --arg yandex_hmac "${YANDEX_TO_CF_EGRESS_HMAC_SECRET}" \
  --arg aws_hmac "${AWS_TO_CF_EGRESS_HMAC_SECRET}" \
  --arg disk "${YANDEX_DISK_TOKEN}" \
  --arg google_service_account "${GOOGLE_SERVICE_ACCOUNT_JSON}" \
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
  --arg ymq_id "${YMQ_ACCESS_KEY_ID:-}" \
  --arg ymq_secret "${YMQ_SECRET_ACCESS_KEY:-}" \
  --arg audience "${YANDEX_OIDC_AUDIENCE:-}" \
  --arg subjects "${YANDEX_SERVICE_ACCOUNT_IDS:-}" \
  '{
    TG_BOT_TOKEN:$token,
    TG_BOT_USERNAME:$telegram_username,
    YANDEX_TO_CF_EGRESS_HMAC_SECRET:$yandex_hmac,
    AWS_TO_CF_EGRESS_HMAC_SECRET:$aws_hmac,
    YANDEX_DISK_TOKEN:$disk,
    GOOGLE_SERVICE_ACCOUNT_JSON:$google_service_account,
    VK_ACCESS_TOKEN:$vk_token,
    VK_CALLBACK_SECRET:$vk_secret,
    VK_CONFIRMATION_STRING:$vk_confirmation,
    VK_GROUP_ID:$vk_group,
    TG_ADMIN_IDS:$tg_admins,
    VK_ADMIN_IDS:$vk_admins,
    TG_GAMEMASTER_IDS:$tg_gamemasters,
    VK_GAMEMASTER_IDS:$vk_gamemasters,
    PARTICIPANT_KEY_HMAC_SECRET:$participant,
    CF_TO_YANDEX_HMAC_SECRET:$ingress
  } + if $active_backend == "aws" then {} else {
    YMQ_ACCESS_KEY_ID:$ymq_id,
    YMQ_SECRET_ACCESS_KEY:$ymq_secret,
    YANDEX_OIDC_AUDIENCE:$audience,
    YANDEX_SERVICE_ACCOUNT_IDS:$subjects
  } end' \
  | npx --yes wrangler@4.125.0 secret bulk \
      --config cloudflare/telegram-egress/wrangler.toml \
      --name "${EGRESS_WORKER_NAME}"
