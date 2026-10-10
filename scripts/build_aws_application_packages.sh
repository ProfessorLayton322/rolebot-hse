#!/usr/bin/env bash
set -euo pipefail

repo_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
build_dir="${repo_dir}/build/aws-application"
output_dir="${repo_dir}/dist"
python_build_dir="${build_dir}/python"
notifier_build_dir="${build_dir}/registration-notifier"
uploader_build_dir="${build_dir}/disk-uploader"

rm -rf "${build_dir}"
mkdir -p "${python_build_dir}" "${notifier_build_dir}" "${uploader_build_dir}" "${output_dir}"

python3 -m pip install \
  --disable-pip-version-check \
  --no-compile \
  --requirement "${repo_dir}/requirements-aws.txt" \
  --target "${python_build_dir}"
cp -R "${repo_dir}/src/larp_bot" "${python_build_dir}/larp_bot"
cp \
  "${repo_dir}/aws_gateway.py" \
  "${repo_dir}/aws_ordered_worker.py" \
  "${repo_dir}/aws_upload_completion.py" \
  "${python_build_dir}/"

find "${python_build_dir}" -type d -name '__pycache__' -prune -exec rm -rf '{}' +
find "${python_build_dir}" -type f -exec touch -t 198001010000 '{}' +
python3 "${repo_dir}/scripts/reproducible_zip.py" \
  "${python_build_dir}" \
  "${output_dir}/aws-application-python.zip"

zip_bytes="$(stat -c '%s' "${output_dir}/aws-application-python.zip")"
unzipped_bytes="$(du -sb "${python_build_dir}" | cut -f1)"
if ((zip_bytes > 50 * 1024 * 1024)); then
  echo "AWS Python package exceeds the 50 MiB direct-upload limit" >&2
  exit 1
fi
if ((unzipped_bytes > 250 * 1024 * 1024)); then
  echo "AWS Python package exceeds the 250 MiB unzipped limit" >&2
  exit 1
fi

CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go -C "${repo_dir}/functions/registration_notifier" build \
  -modfile=aws.go.mod \
  -mod=readonly \
  -tags=aws \
  -trimpath \
  -ldflags="-s -w -buildid=" \
  -o "${notifier_build_dir}/bootstrap" \
  .
chmod 0755 "${notifier_build_dir}/bootstrap"

python3 "${repo_dir}/scripts/reproducible_zip.py" \
  "${notifier_build_dir}" \
  "${output_dir}/aws-registration-notifier.zip" \
  bootstrap

CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go -C "${repo_dir}/functions/disk_uploader" build \
  -modfile=aws.go.mod \
  -mod=readonly \
  -tags=aws \
  -trimpath \
  -ldflags="-s -w -buildid=" \
  -o "${uploader_build_dir}/bootstrap" \
  .
chmod 0755 "${uploader_build_dir}/bootstrap"

python3 "${repo_dir}/scripts/reproducible_zip.py" \
  "${uploader_build_dir}" \
  "${output_dir}/aws-disk-uploader.zip" \
  bootstrap

for archive in aws-registration-notifier.zip aws-disk-uploader.zip; do
  archive_bytes="$(stat -c '%s' "${output_dir}/${archive}")"
  if ((archive_bytes > 50 * 1024 * 1024)); then
    echo "${archive} exceeds the 50 MiB direct-upload limit" >&2
    exit 1
  fi
done

sha256sum \
  "${output_dir}/aws-application-python.zip" \
  "${output_dir}/aws-registration-notifier.zip" \
  "${output_dir}/aws-disk-uploader.zip"
