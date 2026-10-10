from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_cloudflare_router_has_three_fail_closed_modes_and_separate_secrets() -> None:
    ingress = (ROOT / "cloudflare/telegram-ingress/src/index.ts").read_text()
    cloudflare = (ROOT / "infra/terraform/cloudflare.tf").read_text()
    deploy = (ROOT / ".github/workflows/deploy.yml").read_text()
    secret_script = (ROOT / "scripts/update_cloudflare_secrets.sh").read_text()

    for mode in ("yandex", "aws", "maintenance"):
        assert f'"{mode}"' in ingress
    for name in (
        "ACTIVE_BACKEND",
        "YANDEX_BACKEND_URL",
        "AWS_BACKEND_URL",
    ):
        assert name in ingress
        assert name in cloudflare
    for name in (
        "CF_TO_YANDEX_HMAC_SECRET",
        "CF_TO_AWS_HMAC_SECRET",
    ):
        assert name in ingress
        assert name in secret_script
    assert "vars.ACTIVE_BACKEND" in deploy
    assert "vars.AWS_BACKEND_URL" in deploy
    assert "Routing configuration unavailable" in ingress


def test_vk_uses_stable_cloudflare_route_and_keeps_direct_yandex_rollback() -> None:
    outputs = (ROOT / "infra/terraform/outputs.tf").read_text()
    gateway = (ROOT / "infra/terraform/gateway.tf").read_text()
    handler = (ROOT / "src/larp_bot/functions/gateway/handler.py").read_text()

    assert 'value       = "${local.telegram_ingress_url}/vk"' in outputs
    assert 'output "yandex_vk_rollback_url"' in outputs
    assert "/webhooks/vk-cloudflare" in gateway
    assert 'path.endswith("/vk-cloudflare")' in handler
    assert "_verify_cloudflare_vk" in handler
    assert 'if os.getenv("CLOUD_PROVIDER") == "aws"' in handler


def test_egress_accepts_both_migration_signatures_without_exposing_aws_to_yandex() -> None:
    egress = (ROOT / "cloudflare/telegram-egress/src/index.ts").read_text()
    script = (ROOT / "scripts/update_cloudflare_secrets.sh").read_text()

    assert "expectedYandex" in egress
    assert "expectedAws" in egress
    assert "validYandex" in egress
    assert "validAws" in egress
    assert "AWS_TO_CF_EGRESS_HMAC_SECRET:$aws_hmac" in script
    runtime_keys = egress.split("const RUNTIME_CONFIG_KEYS = [", 1)[1].split("] as const", 1)[0]
    assert "AWS_TO_CF_EGRESS_HMAC_SECRET" not in runtime_keys


def test_cloudflare_ownership_does_not_enter_aws_state() -> None:
    aws_terraform = "\n".join(path.read_text() for path in (ROOT / "infra/aws/application").glob("*.tf"))
    assert 'resource "cloudflare_' not in aws_terraform
    assert "cloudflare/cloudflare" not in aws_terraform
