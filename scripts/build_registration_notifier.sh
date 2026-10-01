#!/usr/bin/env bash
set -euo pipefail

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source_dir="${repo_dir}/functions/registration_notifier"
output_dir="${repo_dir}/dist"
output_file="${output_dir}/registration-notifier.zip"

mkdir -p "${output_dir}"
rm -f "${output_file}"
python3 "${repo_dir}/scripts/reproducible_zip.py" "${source_dir}" "${output_file}"
sha256sum "${output_file}"
