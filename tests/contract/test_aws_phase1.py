from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
APPLICATION = ROOT / "infra/aws/application"
BOOTSTRAP = ROOT / "infra/aws-bootstrap"


def _application_terraform() -> str:
    return "\n".join(path.read_text() for path in APPLICATION.glob("*.tf"))


def test_aws_state_boundaries_are_isolated() -> None:
    application = (APPLICATION / "versions.tf").read_text()
    bootstrap = (BOOTSTRAP / "versions.tf").read_text()

    assert 'key          = "larp-bot/aws-production.tfstate"' in application
    assert 'key          = "larp-bot/aws-bootstrap.tfstate"' in bootstrap
    assert "storage.yandexcloud.net" not in application
    assert "storage.yandexcloud.net" not in bootstrap
    assert 'version = "6.68.0"' in application
    assert 'required_version = "= 1.15.9"' in application


def test_bootstrap_root_is_read_only_inventory() -> None:
    terraform = "\n".join(path.read_text() for path in BOOTSTRAP.glob("*.tf"))

    assert not re.search(r'^resource\s+"', terraform, re.MULTILINE)
    assert 'data "aws_iam_openid_connect_provider" "github"' in terraform
    assert 'data "aws_iam_role" "github_plan"' in terraform
    assert 'data "aws_iam_role" "github_deploy"' in terraform
    readme = (BOOTSTRAP / "README.md").read_text()
    assert "BucketOwnerEnforced" not in terraform
    assert "get-bucket-ownership-controls" in readme
    assert "AccountFullAccessRole" in readme


def test_dynamodb_mirror_is_protected_and_within_free_tier_capacity() -> None:
    dynamodb = (APPLICATION / "dynamodb.tf").read_text()
    resources = re.findall(r'resource "aws_dynamodb_table" "([^"]+)"', dynamodb)

    assert resources == ["tg_users", "vk_users", "events", "event_leaders", "registrations"]
    assert len(re.findall(r'billing_mode\s+= "PROVISIONED"', dynamodb)) == 5
    assert dynamodb.count("table_class") == 5
    assert dynamodb.count("deletion_protection_enabled = true") == 5
    assert dynamodb.count("prevent_destroy = true") == 5
    assert dynamodb.count("enabled = false") == 5
    assert dynamodb.count("global_secondary_index") == 4
    # Five tables plus four GSIs at one unit each stay below 25 RCUs/WCUs.
    assert dynamodb.count("read_capacity") == 9
    assert dynamodb.count("write_capacity") == 9


def test_phase_one_standby_controls_cannot_accept_production_traffic() -> None:
    terraform = _application_terraform()
    lambda_config = (APPLICATION / "lambda.tf").read_text()
    scheduler = (APPLICATION / "scheduler.tf").read_text()
    placeholders = "\n".join(
        [
            (APPLICATION / "standby/python/handler.py").read_text(),
            (APPLICATION / "standby/go/main.go").read_text(),
        ]
    )

    assert 'authorization_type = "AWS_IAM"' in lambda_config
    assert re.search(r"aws_lambda_event_source_mapping[^}]+enabled\s+= false", lambda_config, re.DOTALL)
    assert 'state       = "DISABLED"' in scheduler
    assert 'APPLICATION_MODE     = "standby"' in lambda_config
    assert 'MUTATIONS_ENABLED    = "false"' in lambda_config
    assert placeholders.count("standby") >= 2
    assert "vpc_config" not in terraform
    assert "provisioned_concurrent" not in terraform
    assert 'resource "aws_kms_key"' not in terraform
    assert 'resource "aws_secretsmanager_secret"' not in terraform
    assert 'resource "aws_nat_gateway"' not in terraform
    assert "xray" not in terraform.lower()


def test_fifo_has_a_fifo_dlq_and_disabled_partial_batch_consumer() -> None:
    queues = (APPLICATION / "queues.tf").read_text()
    lambda_config = (APPLICATION / "lambda.tf").read_text()

    assert queues.count("fifo_queue                  = true") == 2
    assert "maxReceiveCount     = 5" in queues
    assert "sqs_managed_sse_enabled     = true" in queues
    assert 'function_response_types            = ["ReportBatchItemFailures"]' in lambda_config
    assert "enabled                            = false" in lambda_config


def test_ssm_secrets_never_flow_through_terraform_inputs() -> None:
    terraform = _application_terraform()
    locals_config = (APPLICATION / "locals.tf").read_text()
    variables = (APPLICATION / "variables.tf").read_text()
    workflow = (ROOT / ".github/workflows/aws-deploy.yml").read_text()

    assert 'resource "aws_ssm_parameter"' not in terraform
    assert "parameter_names = {" in locals_config
    assert "parameter_arns = {" in locals_config
    assert "secret" not in variables.lower()
    assert "aws ssm put-parameter" in workflow
    assert "terraform output -json runtime_parameter_names" in workflow
    assert '"${byte_count}" -gt 4096' in workflow
    assert "TF_VAR_" not in workflow


def test_aws_workflows_use_oidc_and_deploy_only_manually() -> None:
    plan = (ROOT / ".github/workflows/aws-plan.yml").read_text()
    deploy = (ROOT / ".github/workflows/aws-deploy.yml").read_text()
    combined = plan + deploy

    action_sha = "aws-actions/configure-aws-credentials@e1253824e5c10ff9df46874f81ed3ec929e19cfd"
    assert combined.count(action_sha) == 2
    assert combined.count("id-token: write") == 2
    assert "AWS_ACCESS_KEY_ID" not in combined
    assert "AWS_SECRET_ACCESS_KEY" not in combined
    assert "workflow_dispatch:" in deploy
    assert "pull_request:" not in deploy
    assert "push:" not in deploy
    assert "DEPLOY_STANDBY" in deploy
    assert "billing_guardrails_confirmed" in deploy


def test_go_standby_package_is_built_as_linux_bootstrap() -> None:
    build = (ROOT / "scripts/build_aws_standby_packages.sh").read_text()
    lambda_config = (APPLICATION / "locals.tf").read_text()

    assert "CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build" in build
    assert '"${go_build_dir}/bootstrap"' in build
    assert lambda_config.count('runtime     = "provided.al2023"') == 2
    assert lambda_config.count('handler     = "bootstrap"') == 2
