from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_ydb_tables_include_registration_source_of_truth() -> None:
    terraform = "\n".join(path.read_text() for path in (ROOT / "infra/terraform").glob("*.tf"))
    resources = re.findall(r'resource\s+"yandex_ydb_table"\s+"([^"]+)"', terraform)
    assert sorted(resources) == ["events", "registrations", "tg_users", "vk_users"]
    ydb = (ROOT / "infra/terraform/ydb.tf").read_text()
    assert 'primary_key = ["event_id", "participant_key"]' in ydb
    assert 'name = "confirmation_deadline"' in ydb
    assert 'name = "vk_profile"' in ydb
    assert 'name = "telegram_profile"' in ydb
    assert 'name = "pass_table_resource_path"' in ydb
    assert 'name = "pass_table_public_url"' in ydb
    assert 'name = "last_bot_buttons_json"' in ydb


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
    assert re.search(
        r'YDB_ENDPOINT\s*=\s*"grpcs://\$\{yandex_ydb_database_serverless\.application\.ydb_api_endpoint\}"',
        functions,
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


def test_yandex_runtime_uses_oidc_protected_worker_config() -> None:
    production = "\n".join(path.read_text() for path in (ROOT / "src/larp_bot").rglob("*.py"))
    terraform = "\n".join(path.read_text() for path in (ROOT / "infra/terraform").glob("*.tf"))
    functions = (ROOT / "infra/terraform/functions.tf").read_text()
    deploy = (ROOT / ".github/workflows/deploy.yml").read_text()
    egress = (ROOT / "cloudflare/telegram-egress/src/index.ts").read_text()

    assert "lockbox" not in production.lower()
    assert "yandex_lockbox" not in terraform
    assert "LOCKBOX_SECRET_ID" not in functions
    assert "RUNTIME_CONFIG_URL" in functions
    assert "update_runtime_secrets" not in deploy
    assert "YANDEX_SERVICE_ACCOUNT_IDS" in deploy
    assert 'const RUNTIME_CONFIG_PATH = "/runtime/config"' in egress
    assert 'const OIDC_ISSUER = "https://auth.yandex.cloud"' in egress
    assert "verifyYandexIdentityToken" in egress


def test_admin_page_size_is_ten_in_shared_engine() -> None:
    source = (ROOT / "src/larp_bot/application/conversation.py").read_text()
    assert "self.events.list_page(after=after, limit=10)" in source


def test_user_models_do_not_contain_character_wish_source_field() -> None:
    models = (ROOT / "src/larp_bot/domain/models.py").read_text()
    user_section = models.split("class UserBase", 1)[1].split("class Event", 1)[0]
    assert "character_wish" not in user_section
