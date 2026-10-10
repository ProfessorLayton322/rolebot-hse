#!/usr/bin/env bash
set -euo pipefail

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
standby_dir="${repo_dir}/infra/aws/application/standby"
build_dir="${repo_dir}/build/aws-standby"
output_dir="${repo_dir}/dist"

python_build_dir="${build_dir}/python"
go_build_dir="${build_dir}/go"

rm -rf "${build_dir}"
mkdir -p "${python_build_dir}" "${go_build_dir}" "${output_dir}"

cp "${standby_dir}/python/handler.py" "${python_build_dir}/handler.py"

CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build \
  -trimpath \
  -ldflags="-s -w -buildid=" \
  -o "${go_build_dir}/bootstrap" \
  "${standby_dir}/go/main.go"
chmod 0755 "${go_build_dir}/bootstrap"

python3 "${repo_dir}/scripts/reproducible_zip.py" \
  "${python_build_dir}" \
  "${output_dir}/aws-standby-python.zip"
python3 "${repo_dir}/scripts/reproducible_zip.py" \
  "${go_build_dir}" \
  "${output_dir}/aws-standby-go.zip" \
  bootstrap

sha256sum \
  "${output_dir}/aws-standby-python.zip" \
  "${output_dir}/aws-standby-go.zip"
