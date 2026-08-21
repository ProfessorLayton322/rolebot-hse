#!/usr/bin/env bash
set -euo pipefail

: "${ACTIONS_ID_TOKEN_REQUEST_TOKEN:?GitHub did not provide an OIDC request token; permissions.id-token must be write}"
: "${ACTIONS_ID_TOKEN_REQUEST_URL:?GitHub did not provide an OIDC request URL; permissions.id-token must be write}"
: "${YC_WIF_AUDIENCE:?YC_WIF_AUDIENCE GitHub Actions variable is required}"
: "${YC_DEPLOY_SERVICE_ACCOUNT_ID:?YC_DEPLOY_SERVICE_ACCOUNT_ID GitHub Actions variable is required}"
: "${GITHUB_ENV:?GITHUB_ENV is required}"

if ! oidc_response="$(curl --fail-with-body --silent --show-error \
  --header "Authorization: bearer ${ACTIONS_ID_TOKEN_REQUEST_TOKEN}" \
  "${ACTIONS_ID_TOKEN_REQUEST_URL}&audience=${YC_WIF_AUDIENCE}")"; then
  echo "::error::GitHub failed to issue an OIDC token for audience ${YC_WIF_AUDIENCE}" >&2
  exit 1
fi

if ! oidc_token="$(printf '%s' "${oidc_response}" | jq -er \
  '.value | select(type == "string" and length > 0)' 2>/dev/null)"; then
  echo "::error::GitHub OIDC response did not contain a token" >&2
  exit 1
fi

# The subject is not secret. Decode it so a failed exchange tells the operator
# the exact value Yandex Cloud must use for its federated credential. This also
# handles GitHub's immutable subject format for newly created repositories.
oidc_payload="${oidc_token#*.}"
oidc_payload="${oidc_payload%%.*}"
case $((${#oidc_payload} % 4)) in
  2) oidc_payload+="==" ;;
  3) oidc_payload+="=" ;;
esac
if oidc_claims="$(printf '%s' "${oidc_payload}" | tr '_-' '/+' | base64 --decode 2>/dev/null)" \
  && oidc_subject="$(printf '%s' "${oidc_claims}" | jq -er \
    '.sub | select(type == "string" and length > 0)' 2>/dev/null)"; then
  :
else
  oidc_subject="<could not decode GitHub OIDC subject>"
fi

if ! exchange_response="$(curl --fail-with-body --silent --show-error \
  --request POST \
  --header 'Content-Type: application/x-www-form-urlencoded' \
  https://auth.yandex.cloud/oauth/token \
  --data-urlencode 'grant_type=urn:ietf:params:oauth:grant-type:token-exchange' \
  --data-urlencode 'requested_token_type=urn:ietf:params:oauth:token-type:access_token' \
  --data-urlencode "audience=${YC_DEPLOY_SERVICE_ACCOUNT_ID}" \
  --data-urlencode "subject_token=${oidc_token}" \
  --data-urlencode 'subject_token_type=urn:ietf:params:oauth:token-type:id_token')"; then
  exchange_error="$(printf '%s' "${exchange_response:-}" | jq -r \
    '[.error, .error_description] | map(select(type == "string" and length > 0)) | join(": ")' \
    2>/dev/null || true)"
  if [[ -z "${exchange_error}" ]]; then
    exchange_error="HTTP request failed"
  fi
  exchange_error="${exchange_error//$'\n'/ }"
  echo "::error::Yandex OIDC token exchange failed: ${exchange_error}" >&2
  echo "::error::Link the deploy service account to this exact GitHub OIDC subject: ${oidc_subject}" >&2
  exit 1
fi

if ! yc_token="$(printf '%s' "${exchange_response}" | jq -er \
  '.access_token | select(type == "string" and length > 0)' 2>/dev/null)"; then
  exchange_error="$(printf '%s' "${exchange_response}" | jq -r \
    '[.error, .error_description] | map(select(type == "string" and length > 0)) | join(": ")' \
    2>/dev/null || true)"
  exchange_error="${exchange_error//$'\n'/ }"
  echo "::error::Yandex OIDC response contained no access token${exchange_error:+: ${exchange_error}}" >&2
  echo "::error::Link the deploy service account to this exact GitHub OIDC subject: ${oidc_subject}" >&2
  exit 1
fi

echo "::add-mask::${yc_token}"
echo "YC_TOKEN=${yc_token}" >>"${GITHUB_ENV}"
