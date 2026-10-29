#!/usr/bin/env bash
# Sourced by smoke_test.sh; callers provide response_file and VK configuration.
source "$(dirname "${BASH_SOURCE[0]}")/smoke_http_helpers.sh"

vk_history_request() {
  local history_status
  history_status="$(smoke_curl "VK message history" --silent --show-error --connect-timeout 5 --max-time 10 \
    --output "${response_file}" --write-out '%{http_code}' \
    --request POST \
    --data-urlencode "access_token=${VK_ACCESS_TOKEN}" \
    --data-urlencode 'v=5.199' \
    --data-urlencode "peer_id=${vk_admin_id}" \
    --data-urlencode 'count=20' \
    'https://api.vk.com/method/messages.getHistory')" || return
  test "${history_status}" = "200" || {
    # VK errors may echo access_token in request_params. Never print the body.
    echo "::error::VK message history returned HTTP ${history_status}" >&2
    return 2
  }
}

vk_latest_message_id() {
  vk_history_request || return
  python3 - "${response_file}" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as stream:
    payload = json.load(stream)
if "error" in payload:
    print(f"VK message history API error code {payload['error'].get('error_code', 'unknown')}", file=sys.stderr)
    raise SystemExit(2)
items = payload["response"]["items"]
print(max((item.get("id", 0) for item in items), default=0))
PY
}

vk_assert_outgoing_after() {
  local previous_id="$1"
  local expected_text="$2"
  local probe="$3"
  local event_id="$4"
  local attempt history_result
  # Cloudflare can acknowledge VK before Lambda finishes (30-second timeout).
  # Check immediately, then allow 38 seconds plus API calls for delivery/history
  # visibility. Poll history only: never replay a user-visible callback.
  for attempt in $(seq 1 20); do
    vk_history_request || return
    if python3 - "${response_file}" "${previous_id}" "${VK_GROUP_ID}" "${expected_text}" "${attempt}" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as stream:
    payload = json.load(stream)
if "error" in payload:
    print(f"VK message history API error code {payload['error'].get('error_code', 'unknown')}", file=sys.stderr)
    raise SystemExit(2)
previous_id, group_id = map(int, sys.argv[2:4])
expected = sys.argv[4]
items = payload["response"]["items"]
new_messages = [item for item in items if item.get("id", 0) > previous_id]
outgoing = [item for item in new_messages if item.get("out") == 1 and item.get("from_id") == -group_id]
if any(expected in item.get("text", "") for item in outgoing):
    raise SystemExit(0)
if sys.argv[5] == "20":
    # Counts and IDs distinguish missing delivery from unexpected response text
    # without exposing message bodies, profiles, or the VK access token.
    print(json.dumps({
        "previous_message_id": previous_id,
        "latest_message_id": max((item.get("id", 0) for item in items), default=0),
        "history_items": len(items),
        "new_messages": len(new_messages),
        "new_community_outgoing": len(outgoing),
        "expected_text_found": False,
    }), file=sys.stderr)
raise SystemExit(1)
PY
    then
      echo "VK ${probe} delivery verified"
      return 0
    else
      history_result=$?
    fi
    if test "${history_result}" -ne 1; then
      echo "::error::VK ${probe} history check failed (event_id=${event_id})" >&2
      return "${history_result}"
    fi
    if test "${attempt}" -lt 20; then
      sleep 2
    fi
  done
  echo "::error::VK ${probe}: expected bot response was not found after 20 history checks (event_id=${event_id})" >&2
  return 1
}
