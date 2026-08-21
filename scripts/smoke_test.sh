#!/usr/bin/env bash
set -euo pipefail

: "${VK_CALLBACK_URL:?VK_CALLBACK_URL is required}"
response_file="$(mktemp)"
trap 'rm -f "${response_file}"' EXIT

status="$(curl --silent --show-error --output "${response_file}" --write-out '%{http_code}' \
  --request POST \
  --header 'Content-Type: application/json' \
  --data '{}' \
  "${VK_CALLBACK_URL}")"
if test "${status}" = "403" || test "${status}" = "400"; then
  echo "Gateway rejects an unauthenticated callback as expected (${status})"
  exit 0
fi

response_body="$(head --bytes 1000 "${response_file}")"
echo "::error::Gateway returned unexpected HTTP ${status}: ${response_body}" >&2
exit 1
