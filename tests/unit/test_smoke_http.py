from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

HELPERS = Path(__file__).resolve().parents[2] / "scripts" / "smoke_http_helpers.sh"
SECRET = "private-token-and-payload"


def run_http(tmp_path: Path, codes: list[int], *, http_status: str = "200") -> subprocess.CompletedProcess[str]:
    for index, code in enumerate(codes, start=1):
        (tmp_path / f"code-{index}").write_text(str(code))
    script = """
set -euo pipefail
source "$1"
fixture_dir="$2"
curl() {
  local count=0
  if test -f "${fixture_dir}/calls"; then count=$(cat "${fixture_dir}/calls"); fi
  count=$((count + 1))
  echo "${count}" > "${fixture_dir}/calls"
  printf '%s\\n' "$@" > "${fixture_dir}/args-${count}"
  local code
  code=$(cat "${fixture_dir}/code-${count}")
  if test "${code}" -ne 0; then
    printf '000'
    return "${code}"
  fi
  printf '%s' "${FIXTURE_HTTP_STATUS}"
}
sleep() { echo "$1" >> "${fixture_dir}/sleeps"; }
smoke_curl 'VK message history' --silent --show-error --write-out '%{http_code}' \\
  --data-urlencode "access_token=${FIXTURE_SECRET}" https://api.vk.com/method/messages.getHistory
"""
    return subprocess.run(  # noqa: S603
        ["bash", "-c", script, "smoke-http-test", str(HELPERS), str(tmp_path)],  # noqa: S607
        env=os.environ | {"FIXTURE_HTTP_STATUS": http_status, "FIXTURE_SECRET": SECRET},
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )


@pytest.mark.parametrize("codes", [[0], [35, 0], [35, 35, 0]])
def test_smoke_http_recovers_from_tls_handshake_reset_without_corrupting_status(
    tmp_path: Path, codes: list[int]
) -> None:
    result = run_http(tmp_path, codes)

    assert result.returncode == 0, result.stderr
    assert result.stdout == "200"
    assert (tmp_path / "calls").read_text().strip() == str(len(codes))
    assert "Smoke HTTP: VK message history" in result.stderr
    assert SECRET not in result.stdout + result.stderr
    first_args = (tmp_path / "args-1").read_text()
    assert "--connect-timeout\n5\n--max-time\n40\n" in first_args
    for attempt in range(2, len(codes) + 1):
        assert (tmp_path / f"args-{attempt}").read_text() == first_args
    if len(codes) > 1:
        assert (tmp_path / "sleeps").read_text().splitlines() == ["1"] * (len(codes) - 1)
    else:
        assert not (tmp_path / "sleeps").exists()


def test_smoke_http_keeps_persistent_tls_failure_and_stops_after_three_attempts(tmp_path: Path) -> None:
    result = run_http(tmp_path, [35, 35, 35, 0])

    assert result.returncode == 35
    assert result.stdout == ""
    assert (tmp_path / "calls").read_text().strip() == "3"
    assert (tmp_path / "sleeps").read_text().splitlines() == ["1", "1"]
    assert "::error::VK message history: curl failed with exit code 35 (attempt 3)" in result.stderr
    assert SECRET not in result.stderr


@pytest.mark.parametrize("code", [6, 22, 28, 52, 55, 56, 60])
def test_smoke_http_preserves_other_errors_without_replaying_potentially_delivered_request(
    tmp_path: Path, code: int
) -> None:
    result = run_http(tmp_path, [code, 0])

    assert result.returncode == code
    assert result.stdout == ""
    assert (tmp_path / "calls").read_text().strip() == "1"
    assert not (tmp_path / "sleeps").exists()
    assert f"curl failed with exit code {code} (attempt 1)" in result.stderr


@pytest.mark.parametrize("http_status", ["200", "403", "500"])
def test_smoke_http_leaves_http_status_validation_to_caller(tmp_path: Path, http_status: str) -> None:
    result = run_http(tmp_path, [0], http_status=http_status)

    assert result.returncode == 0
    assert result.stdout == http_status
    assert (tmp_path / "calls").read_text().strip() == "1"
    assert not (tmp_path / "sleeps").exists()
