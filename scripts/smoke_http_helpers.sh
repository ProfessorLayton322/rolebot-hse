#!/usr/bin/env bash
# Keep HTTP status on stdout; identify probes on stderr without logging secrets.
smoke_curl() {
  local probe="$1"
  shift
  local attempt output result
  echo "Smoke HTTP: ${probe}" >&2
  for attempt in 1 2 3; do
    if output="$(curl --connect-timeout 5 --max-time 40 "$@")"; then
      printf '%s' "${output}"
      return 0
    else
      result=$?
    fi
    # Exit 35 is a TLS handshake failure, before the HTTP request is sent.
    # A timeout or receive error can happen after delivery: never replay those.
    if test "${result}" -ne 35 || test "${attempt}" -eq 3; then
      echo "::error::${probe}: curl failed with exit code ${result} (attempt ${attempt})" >&2
      return "${result}"
    fi
    echo "::warning::${probe}: TLS handshake failed; retrying (${attempt}/3)" >&2
    sleep 1
  done
}
