from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_exactly_three_ydb_tables() -> None:
    terraform = "\n".join(path.read_text() for path in (ROOT / "infra/terraform").glob("*.tf"))
    resources = re.findall(r'resource\s+"yandex_ydb_table"\s+"([^"]+)"', terraform)
    assert sorted(resources) == ["events", "tg_users", "vk_users"]


def test_no_fifo_native_trigger() -> None:
    trigger = (ROOT / "infra/terraform/ymq.tf").read_text()
    assert "queue_id           = yandex_message_queue.worker_kicks.arn" in trigger
    assert "queue_id           = yandex_message_queue.registration_commands" not in trigger


def test_no_direct_telegram_connection_from_yandex_python() -> None:
    production = "\n".join(path.read_text() for path in (ROOT / "src/larp_bot").rglob("*.py"))
    assert "api.telegram.org" not in production
    assert "start_polling" not in production


def test_cloudflare_workers_are_separate() -> None:
    ingress = ROOT / "cloudflare/telegram-ingress/src/index.ts"
    egress = ROOT / "cloudflare/telegram-egress/src/index.ts"
    assert ingress.exists() and egress.exists()
    assert "api.telegram.org" not in ingress.read_text()
    assert "api.telegram.org" in egress.read_text()


def test_runtime_ydb_endpoint_enables_tls() -> None:
    functions = (ROOT / "infra/terraform/functions.tf").read_text()
    assert (
        'YDB_ENDPOINT              = "grpcs://${yandex_ydb_database_serverless.application.ydb_api_endpoint}"'
        in functions
    )


def test_cloudflare_workers_keep_public_subdomains_enabled() -> None:
    terraform = (ROOT / "infra/terraform/cloudflare.tf").read_text()
    assert terraform.count("subdomain = {") == 2
    # Two primary Worker resources and two dedicated subdomain resources must
    # agree; otherwise the provider oscillates the public routes back to false.
    assert terraform.count("enabled          = true") == 4
    for worker in ("telegram-ingress", "telegram-egress"):
        config = (ROOT / f"cloudflare/{worker}/wrangler.toml").read_text()
        assert "workers_dev = true" in config


def test_cloudflare_secret_updates_load_worker_configs() -> None:
    script = (ROOT / "scripts/update_cloudflare_secrets.sh").read_text()
    assert "--config cloudflare/telegram-ingress/wrangler.toml" in script
    assert "--config cloudflare/telegram-egress/wrangler.toml" in script


def test_admin_page_size_is_ten_in_shared_engine() -> None:
    source = (ROOT / "src/larp_bot/application/conversation.py").read_text()
    assert "self.events.list_page(after=after, limit=10)" in source


def test_user_models_do_not_contain_character_wish_source_field() -> None:
    models = (ROOT / "src/larp_bot/domain/models.py").read_text()
    user_section = models.split("class UserBase", 1)[1].split("class Event", 1)[0]
    assert "character_wish" not in user_section
