from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

HELPERS = Path(__file__).resolve().parents[2] / "scripts" / "vk_smoke_helpers.sh"
EXPECTED = "Привет! Этот бот поможет зарегистрироваться"
SECRET = "must-not-appear-in-logs"


def history(*items: dict[str, object]) -> dict[str, object]:
    return {"response": {"items": list(items)}}


def message(message_id: int, *, text: str = EXPECTED, out: int = 1, from_id: int = -42) -> dict[str, object]:
    return {"id": message_id, "out": out, "from_id": from_id, "text": text}


def run_helpers(
    tmp_path: Path,
    responses: list[dict[str, object]],
    *,
    mode: str = "poll",
    http_status: str = "200",
) -> subprocess.CompletedProcess[str]:
    for index, payload in enumerate(responses, start=1):
        (tmp_path / f"history-{index}.json").write_text(json.dumps(payload), encoding="utf-8")
    script = """
set -euo pipefail
source "$1"
fixture_dir="$2"
response_file="${fixture_dir}/response.json"
vk_admin_id=100
curl() {
  local count=0
  if test -f "${fixture_dir}/calls"; then count=$(cat "${fixture_dir}/calls"); fi
  count=$((count + 1))
  echo "${count}" > "${fixture_dir}/calls"
  local fixture="${fixture_dir}/history-${count}.json"
  if ! test -f "${fixture}"; then fixture="${fixture_dir}/history-${FIXTURE_COUNT}.json"; fi
  cp "${fixture}" "${response_file}"
  printf '%s' "${FIXTURE_HTTP_STATUS}"
}
sleep() { echo "$1" >> "${fixture_dir}/sleeps"; }
if test "$3" = snapshot; then
  vk_latest_message_id
else
  vk_assert_outgoing_after 10 "$4" /start smoke-test-event
fi
"""
    return subprocess.run(  # noqa: S603
        ["bash", "-c", script, "vk-smoke-test", str(HELPERS), str(tmp_path), mode, EXPECTED],  # noqa: S607
        env=os.environ
        | {
            "VK_GROUP_ID": "42",
            "VK_ACCESS_TOKEN": SECRET,
            "FIXTURE_COUNT": str(len(responses)),
            "FIXTURE_HTTP_STATUS": http_status,
        },
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )


@pytest.mark.parametrize("arrival_poll", [1, 7, 20])
def test_vk_smoke_accepts_immediate_and_delayed_delivery(tmp_path: Path, arrival_poll: int) -> None:
    result = run_helpers(tmp_path, [history(message(10))] * (arrival_poll - 1) + [history(message(11))])

    assert result.returncode == 0, result.stderr
    assert (tmp_path / "calls").read_text().strip() == str(arrival_poll)
    if arrival_poll > 1:
        assert (tmp_path / "sleeps").read_text().splitlines() == ["2"] * (arrival_poll - 1)
    else:
        assert not (tmp_path / "sleeps").exists()
    assert "VK /start delivery verified" in result.stdout


@pytest.mark.parametrize(
    "items, outgoing_count",
    [
        ([], 0),
        ([message(10)], 0),
        ([message(11, out=0, from_id=100)], 0),
        ([message(11, from_id=-99)], 0),
        ([message(11, text="private unexpected answer")], 1),
    ],
)
def test_vk_smoke_still_requires_new_expected_community_reply(
    tmp_path: Path, items: list[dict[str, object]], outgoing_count: int
) -> None:
    result = run_helpers(tmp_path, [history(*items)])

    assert result.returncode == 1
    assert (tmp_path / "calls").read_text().strip() == "20"
    assert len((tmp_path / "sleeps").read_text().splitlines()) == 19
    assert "VK /start" in result.stderr
    assert "event_id=smoke-test-event" in result.stderr
    assert f'"new_community_outgoing": {outgoing_count}' in result.stderr
    assert "private unexpected answer" not in result.stderr
    assert SECRET not in result.stderr


@pytest.mark.parametrize("mode", ["snapshot", "poll"])
def test_vk_history_api_errors_stop_immediately_without_leaking_token(tmp_path: Path, mode: str) -> None:
    payload = {"error": {"error_code": 5, "request_params": [{"key": "access_token", "value": SECRET}]}}
    result = run_helpers(tmp_path, [payload], mode=mode)

    assert result.returncode == 2
    assert (tmp_path / "calls").read_text().strip() == "1"
    assert "API error code 5" in result.stderr
    assert SECRET not in result.stderr
    assert not (tmp_path / "sleeps").exists()


def test_vk_history_http_error_does_not_dump_response_body(tmp_path: Path) -> None:
    result = run_helpers(tmp_path, [{"access_token": SECRET}], http_status="503")

    assert result.returncode == 2
    assert "HTTP 503" in result.stderr
    assert SECRET not in result.stderr
    assert not (tmp_path / "sleeps").exists()


def test_vk_history_snapshot_records_latest_message_before_callback(tmp_path: Path) -> None:
    result = run_helpers(tmp_path, [history(message(12), message(10))], mode="snapshot")

    assert result.returncode == 0
    assert result.stdout.strip() == "12"
