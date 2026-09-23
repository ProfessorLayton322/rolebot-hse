#!/usr/bin/env bash
set -euo pipefail

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source_dir="${repo_dir}/functions/disk_uploader"
output_dir="${repo_dir}/dist"
output_file="${output_dir}/disk-uploader.zip"

mkdir -p "${output_dir}"
rm -f "${output_file}"
python3 "${repo_dir}/scripts/reproducible_zip.py" "${source_dir}" "${output_file}"
sha256sum "${output_file}"
