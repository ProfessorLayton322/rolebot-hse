from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_ydb_tables_include_registration_source_of_truth() -> None:
    terraform = "\n".join(path.read_text() for path in (ROOT / "infra/terraform").glob("*.tf"))
    resources = re.findall(r'resource\s+"yandex_ydb_table"\s+"([^"]+)"', terraform)
    assert sorted(resources) == ["event_leaders", "events", "registrations", "tg_users", "vk_users"]
    ydb = (ROOT / "infra/terraform/ydb.tf").read_text()
    assert 'primary_key = ["event_id", "participant_key"]' in ydb
    assert 'name = "confirmation_deadline"' in ydb
    assert 'name = "registration_open_at"' in ydb
    assert 'name = "registration_open_notified_recipients_json"' in ydb
    assert 'name = "registration_open_notification_completed_at"' in ydb
    assert 'name = "signup_token"' in ydb
    assert 'name = "telegram_post_url"' in ydb
    assert 'name = "telegram_chat_id"' in ydb
    assert 'name = "telegram_chat_invite_link"' in ydb
    assert 'name = "vk_post_url"' in ydb
    assert 'name = "vk_profile"' in ydb
    assert 'name = "telegram_profile"' in ydb
    assert '{ name = "nickname", type = "Utf8", not_null = false }' in ydb
    assert ydb.count('name = "nickname"') == 2
    assert 'name = "public_table_resource_path"' in ydb
    assert 'name = "public_table_public_url"' in ydb
    assert 'name = "pass_table_resource_path"' in ydb
    assert 'name = "pass_table_public_url"' in ydb
    assert 'name = "achievement_resource_path"' in ydb
    assert 'name = "achievement_public_url"' in ydb
    assert 'name = "last_bot_buttons_json"' in ydb
    assert 'name = "is_gamemaster"' in ydb
    assert 'name = "privileged_role"' in ydb
    assert 'name = "gamemaster_grant_operation_id"' in ydb
    assert 'name = "confirmed_notifications_json"' in ydb
    assert 'name = "last_confirmed_notification_operation_id"' in ydb
    assert 'name = "player_amount"' in ydb
    assert 'name = "game_date"' in ydb
    assert 'name = "last_reserve_promotion_operation_id"' in ydb
    assert 'name = "last_reserve_promotion_participant_key"' in ydb
    assert 'name = "last_reserve_promotion_delivered_operation_id"' in ydb
    assert 'name = "last_confirmed_link_promotion_operation_id"' in ydb
    assert 'name = "last_confirmed_link_promotion_participant_key"' in ydb
    assert 'name = "last_confirmed_link_promotion_delivered_operation_id"' in ydb
    assert 'name = "last_participant_removal_operation_id"' in ydb
    assert 'name = "participant_removal_keys_json"' in ydb
    assert 'name = "participant_removal_notified_keys_json"' in ydb
    assert 'name = "archived_at"' in ydb
    assert 'primary_key = ["event_id", "platform", "platform_user_id"]' in ydb
    assert ydb.count('name = "telegram_handle"') == 2
    assert ydb.count('name = "linked_vk_id"') == 1


def test_no_fifo_native_trigger() -> None:
    trigger = (ROOT / "infra/terraform/ymq.tf").read_text()
    assert "queue_id           = yandex_message_queue.worker_kicks.arn" in trigger
    assert "queue_id           = yandex_message_queue.registration_commands" not in trigger


def test_scheduled_registration_notifications_use_an_isolated_timer_function() -> None:
    functions = (ROOT / "infra/terraform/functions.tf").read_text()
    triggers = (ROOT / "infra/terraform/ymq.tf").read_text()
    build = (ROOT / "scripts/build_function.sh").read_text()

    assert 'entrypoint         = "registration_notifier.handler"' in functions
    assert 'cron_expression = "* * * * ? *"' in triggers
    assert "yandex_function.registration_notifier.id" in triggers
    assert "registration_notifier.py" in build


def test_yandex_disk_uploads_run_asynchronously_in_the_low_memory_go_function() -> None:
    python_disk = "\n".join(path.read_text() for path in (ROOT / "src/larp_bot/adapters/yandex_disk").glob("*.py"))
    staging = (ROOT / "src/larp_bot/adapters/yandex_disk/uploader.py").read_text()
    go_uploader = (ROOT / "functions/disk_uploader/index.go").read_text()
    functions = (ROOT / "infra/terraform/functions.tf").read_text()
    iam = (ROOT / "infra/terraform/iam.tf").read_text()
    storage = (ROOT / "infra/terraform/storage.tf").read_text()

    assert "resources/upload" not in python_disk
    assert "resources/publish" not in python_disk
    assert '"?integration=async"' in staging
    assert 'runtime            = "golang123"' in functions
    assert re.search(r'resource "yandex_function" "disk_uploader" \{.*?memory\s+= 128', functions, re.DOTALL)
    assert 'trigger_invoker = { role = "functions.functionInvoker"' in iam
    assert 'resource "time_sleep" "async_invoker_ready"' in iam
    assert functions.count("time_sleep.async_invoker_ready") == 2
    assert 'async_function_revision = "async-iam-ready-v1"' in functions
    assert functions.count("local.async_function_revision") == 2
    assert 'stagingPrefix    = "larp-bot/uploads/"' in go_uploader
    assert "resources/upload" in go_uploader
    assert "resources/publish" in go_uploader
    assert "deleteStaged" in go_uploader
    assert 'id      = "expire-abandoned-disk-uploads"' in storage


def test_deployment_packages_the_read_only_achievement_template() -> None:
    deploy = (ROOT / ".github/workflows/deploy.yml").read_text()
    fetcher = (ROOT / "scripts/fetch_achievement_template.py").read_text()

    assert "python scripts/fetch_achievement_template.py" in deploy
    assert "larp-bot/roleclub_achievement_basic.pptx" in deploy
    assert deploy.index("python scripts/fetch_achievement_template.py") < deploy.index("bash scripts/build_function.sh")
    assert "get_object" in fetcher
    assert "put_object" not in fetcher


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


def test_button_lists_use_shared_five_item_page_size() -> None:
    source = (ROOT / "src/larp_bot/application/conversation.py").read_text()
    assert "async def _show_admin_games(" in source
    assert "archived=False" in source
    assert "PAGE_SIZE = 5" in source
    assert "limit=PAGE_SIZE" in source
    assert 'label="⬅️ Предыдущая"' not in source
    assert 'label="➡️ Следующая"' not in source
    assert 'label="➡️ Далее"' not in source


def test_gamemaster_ids_are_propagated_to_runtime_config() -> None:
    names = ("TG_GAMEMASTER_IDS", "VK_GAMEMASTER_IDS")
    deploy = (ROOT / ".github/workflows/deploy.yml").read_text()
    update_script = (ROOT / "scripts/update_cloudflare_secrets.sh").read_text()
    worker = (ROOT / "cloudflare/telegram-egress/src/index.ts").read_text()
    wrangler = (ROOT / "cloudflare/telegram-egress/wrangler.toml").read_text()

    for name in names:
        assert f"vars.{name}" in deploy
        assert f'validate_numeric_ids "{name}"' in update_script
        assert name in worker
        assert name in wrangler


def test_user_models_do_not_contain_character_wish_source_field() -> None:
    models = (ROOT / "src/larp_bot/domain/models.py").read_text()
    user_section = models.split("class UserBase", 1)[1].split("class Event", 1)[0]
    assert "character_wish" not in user_section


def test_rate_limits_are_wired_into_telegram_and_yandex() -> None:
    variables = (ROOT / "infra/terraform/variables.tf").read_text()
    cloudflare = (ROOT / "infra/terraform/cloudflare.tf").read_text()
    webhook = (ROOT / "scripts/configure_telegram_webhook.sh").read_text()
    ingress = (ROOT / "cloudflare/telegram-ingress/src/index.ts").read_text()

    assert re.search(r'variable "webhook_rate_limit_rps" \{\s+type\s+= number\s+default = 10\n', variables)
    assert "max_connections=5" in webhook
    ingress_version = cloudflare.split('resource "cloudflare_worker_version" "telegram_ingress"', 1)[1]
    ingress_version = ingress_version.split("\n}\n", 1)[0]
    assert 'type         = "ratelimit"' in ingress_version
    assert 'name         = "TG_USER_RATE_LIMITER"' in ingress_version
    assert "limit  = var.telegram_user_rate_limit_per_minute" in ingress_version
    assert "period = 60" in ingress_version
    assert "env.TG_USER_RATE_LIMITER" in ingress
