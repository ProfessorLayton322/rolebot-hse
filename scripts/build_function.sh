#!/usr/bin/env bash
set -euo pipefail

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
build_dir="$(mktemp -d)"
package_dir="${build_dir}/package"
output_dir="${repo_dir}/dist"
output_file="${output_dir}/function-package.zip"
trap 'rm -rf "${build_dir}"' EXIT

mkdir -p "${package_dir}" "${output_dir}"
python3 -m pip install \
  --disable-pip-version-check \
  --no-compile \
  --requirement "${repo_dir}/requirements.txt" \
  --target "${package_dir}"
cp -R "${repo_dir}/src/larp_bot" "${package_dir}/larp_bot"
cp "${repo_dir}/main.py" "${repo_dir}/ordered_worker.py" "${package_dir}/"

find "${package_dir}" -type d -name '__pycache__' -prune -exec rm -rf '{}' +
find "${package_dir}" -type f -exec touch -t 198001010000 '{}' +

rm -f "${output_file}"
python3 "${repo_dir}/scripts/reproducible_zip.py" "${package_dir}" "${output_file}"
sha256sum "${output_file}"
