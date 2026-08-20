#!/usr/bin/env bash
set -euo pipefail

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if rg -n 'api\.telegram\.org|start_polling|getUpdates' "${repo_dir}/src/larp_bot"; then
  echo "Production Yandex Python code must not connect directly to Telegram" >&2
  exit 1
fi
echo "Telegram transport isolation OK"

