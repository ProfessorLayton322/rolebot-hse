from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
terraform = "\n".join(path.read_text() for path in (ROOT / "infra/terraform").glob("*.tf"))
resource_pattern = re.compile(r'resource\s+"yandex_ydb_table"\s+"([^"]+)"\s*\{')
resources = resource_pattern.findall(terraform)
expected = ["event_leaders", "events", "registrations", "tg_users", "vk_users"]
if sorted(resources) != expected:
    raise SystemExit(
        f"Expected tg_users, vk_users, events, event_leaders, and registrations YDB tables; found {resources}"
    )

path_pattern = re.compile(r'\bpath\s*=\s*"([^"]+)"')
ydb_file = (ROOT / "infra/terraform/ydb.tf").read_text()
paths = [
    value
    for value in path_pattern.findall(ydb_file)
    if value in {"tg_users", "vk_users", "events", "event_leaders", "registrations"}
]
if sorted(paths) != expected:
    raise SystemExit(f"Unexpected application YDB paths: {paths}")
print("YDB storage constraint OK: tg_users, vk_users, events, event_leaders, registrations")
