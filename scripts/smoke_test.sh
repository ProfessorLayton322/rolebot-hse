#!/usr/bin/env bash
set -euo pipefail

: "${VK_CALLBACK_URL:?VK_CALLBACK_URL is required}"
status="$(curl --silent --output /dev/null --write-out '%{http_code}' \
  --request POST \
  --header 'Content-Type: application/json' \
  --data '{}' \
  "${VK_CALLBACK_URL}")"
test "${status}" = "403" || test "${status}" = "400"
echo "Gateway rejects an unauthenticated callback as expected (${status})"
