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
    assert dynamodb.count("global_secondary_index") == 5
    # Five tables plus five GSIs at one unit each stay below 25 RCUs/WCUs.
    assert dynamodb.count("read_capacity") == 10
    assert dynamodb.count("write_capacity") == 10


def test_standby_controls_cannot_accept_production_traffic() -> None:
    terraform = _application_terraform()
    lambda_config = (APPLICATION / "lambda.tf").read_text()
    scheduler = (APPLICATION / "scheduler.tf").read_text()
    variables = (APPLICATION / "variables.tf").read_text()
    placeholders = "\n".join(
        [
            (APPLICATION / "standby/python/handler.py").read_text(),
            (APPLICATION / "standby/go/main.go").read_text(),
        ]
    )

    assert 'authorization_type = "NONE"' in lambda_config
    assert 'action                 = "lambda:InvokeFunctionUrl"' in lambda_config
    assert "invoked_via_function_url = true" in lambda_config
    assert "enabled                            = var.sqs_event_mapping_enabled" in lambda_config
    assert 'state       = var.scheduler_enabled ? "ENABLED" : "DISABLED"' in scheduler
    assert "APPLICATION_MODE = (" in lambda_config
    assert "var.application_mode" in lambda_config
    assert "var.mutations_enabled" in lambda_config
    assert re.search(r'variable "application_mode"[^}]+default\s+= "standby"', variables, re.DOTALL)
    for control in ("mutations_enabled", "sqs_event_mapping_enabled", "scheduler_enabled"):
        assert re.search(rf'variable "{control}"[^}}]+default\s+= false', variables, re.DOTALL)
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
    assert "enabled                            = var.sqs_event_mapping_enabled" in lambda_config
    assert "batch_size                         = 1" in lambda_config
    assert 'reserved_concurrent_executions = each.key == "ordered_worker" ? 1 : -1' in lambda_config


def test_ordered_worker_can_publish_continuation_commands() -> None:
    iam = (APPLICATION / "iam.tf").read_text()
    publish_statement = iam.split('sid       = "PublishRegistrationCommands"', 1)[0].rsplit('dynamic "statement" {', 1)[
        1
    ]

    assert 'contains(["gateway", "ordered_worker", "registration_notifier"], each.key)' in publish_statement
    assert 'actions   = ["sqs:GetQueueAttributes", "sqs:GetQueueUrl", "sqs:SendMessage"]' in iam


def test_aws_dashboard_covers_compute_errors_and_retry_storms() -> None:
    monitoring = (APPLICATION / "monitoring.tf").read_text()
    lambda_config = (APPLICATION / "lambda.tf").read_text()
    outputs = (APPLICATION / "outputs.tf").read_text()

    assert 'resource "aws_cloudwatch_dashboard" "operations"' in monitoring
    assert 'title   = "Lambda compute burn rate (GB-s/s)"' in monitoring
    assert '"Duration"' in monitoring
    assert "/ 1024 / 1000 / ${local.dashboard_period_seconds}" in monitoring
    assert 'title   = "Lambda invocation error rate (%)"' in monitoring
    assert "100 * FILL(errors_${index}, 0) / inv_${index}" in monitoring

    for metric in (
        "Errors",
        "Throttles",
        "DeadLetterErrors",
        "DestinationDeliveryFailures",
        "AsyncEventsDropped",
        "RecursiveInvocationsDropped",
        "Url4xxCount",
        "Url5xxCount",
        "AsyncEventAge",
    ):
        assert f'"{metric}"' in monitoring

    for metric in (
        "NumberOfMessagesReceived",
        "NumberOfMessagesDeleted",
        "NumberOfDeduplicatedSentMessages",
        "ApproximateAgeOfOldestMessage",
        "PolledEventCount",
        "InvokedEventCount",
        "DeletedEventCount",
        "FailedInvokeEventCount",
    ):
        assert f'"{metric}"' in monitoring

    assert 'resource "aws_cloudwatch_metric_alarm" "registration_retry_storm"' in monitoring
    assert "IF(invoked >= 5, invoked / IF(deleted > 0, deleted, 1), 0)" in monitoring
    assert "evaluation_periods  = 2" in monitoring
    assert "datapoints_to_alarm = 2" in monitoring
    assert 'metrics = ["EventCount"]' in lambda_config

    for metric in (
        "TargetErrorCount",
        "TargetErrorThrottledCount",
        "InvocationThrottleCount",
        "InvocationDroppedCount",
        "SystemErrors",
        "UserErrors",
        "ReadThrottleEvents",
        "WriteThrottleEvents",
        "TransactionConflict",
    ):
        assert metric in monitoring

    assert "Recent application and runtime errors" in monitoring
    assert "SOURCE logGroups(namePrefix:" in monitoring
    assert 'output "cloudwatch_operations_dashboard_url"' in outputs


def test_generated_cloudwatch_metrics_preserve_row_arrays() -> None:
    monitoring = (APPLICATION / "monitoring.tf").read_text()
    terraform_test = (APPLICATION / "tests/monitoring.tftest.hcl").read_text()
    workflow = (ROOT / ".github/workflows/ci.yml").read_text()

    assert "flatten([" not in monitoring
    for generated_rows in (
        "lambda_compute_rate_metrics",
        "lambda_error_rate_metrics",
        "lambda_failure_count_metrics",
        "dynamodb_retry_metrics",
    ):
        assert re.search(rf"{generated_rows}\s*=\s*concat\(\[", monitoring)
        assert f"[for row in local.{generated_rows} : can(row[0])]" in monitoring
    assert 'check "dashboard_generated_metrics_are_rows"' in monitoring
    assert "jsondecode(aws_cloudwatch_dashboard.operations.dashboard_body).widgets" in terraform_test
    assert "can(row[0])" in terraform_test
    assert "- run: terraform test" in workflow


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
    assert "TF_VAR_CF_TO_AWS_HMAC_SECRET" not in workflow
    assert "TF_VAR_AWS_TO_CF_EGRESS_HMAC_SECRET" not in workflow


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
    assert "APPLY_AWS_CONTROLS" in deploy
    assert "billing_guardrails_confirmed" in deploy


def test_aws_deployments_validate_signed_google_drive_gateway_before_apply() -> None:
    production = (ROOT / ".github/workflows/deploy.yml").read_text()
    manual = (ROOT / ".github/workflows/aws-deploy.yml").read_text()

    for workflow in (production, manual):
        assert workflow.index("Validate signed Google Drive gateway") < workflow.index("terraform apply")
        assert "integrations/google-drive-apps-script/check_endpoint.py" in workflow
        assert "GOOGLE_APPS_SCRIPT_HMAC_SECRET" in workflow
        assert "GOOGLE_APPS_SCRIPT_URL" in workflow


def test_go_aws_packages_are_built_as_linux_bootstraps() -> None:
    build = (ROOT / "scripts/build_aws_application_packages.sh").read_text()
    lambda_config = (APPLICATION / "locals.tf").read_text()

    assert build.count("CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go") == 2
    assert '"${notifier_build_dir}/bootstrap"' in build
    assert '"${uploader_build_dir}/bootstrap"' in build
    assert build.count("-tags=aws") == 2
    assert build.count("-modfile=aws.go.mod") == 2
    assert "aws-registration-notifier.zip" in build
    assert "aws-disk-uploader.zip" in build
    assert lambda_config.count('runtime     = "provided.al2023"') == 2
    assert lambda_config.count('handler     = "bootstrap"') == 2

    workflows = "\n".join(
        (ROOT / path).read_text()
        for path in (
            ".github/workflows/ci.yml",
            ".github/workflows/aws-plan.yml",
            ".github/workflows/aws-deploy.yml",
        )
    )
    assert 'go-version: "1.26.9"' in workflows


def test_phase_two_handlers_remain_hard_gated_in_standby() -> None:
    lambda_config = (APPLICATION / "lambda.tf").read_text()
    locals_config = (APPLICATION / "locals.tf").read_text()
    runtime_gate = (ROOT / "src/larp_bot/functions/runtime_mode.py").read_text()
    build = (ROOT / "scripts/build_aws_application_packages.sh").read_text()
    variables = (APPLICATION / "variables.tf").read_text()

    assert "var.application_mode" in lambda_config
    assert "var.mutations_enabled" in lambda_config
    assert re.search(r'variable "application_mode"[^}]+default\s+= "standby"', variables, re.DOTALL)
    assert re.search(r'variable "mutations_enabled"[^}]+default\s+= false', variables, re.DOTALL)
    assert 'CLOUD_PROVIDER               = "aws"' in lambda_config
    assert 'handler     = "aws_gateway.handler"' in locals_config
    assert 'handler     = "aws_ordered_worker.handler"' in locals_config
    assert 'handler     = "aws_upload_completion.handler"' in locals_config
    assert "mode is ApplicationMode.ACTIVE and self.mutations_enabled" in runtime_gate
    assert "requirements-aws.txt" in build
    assert "50 * 1024 * 1024" in build
    assert "250 * 1024 * 1024" in build


def test_phase_two_go_ports_use_aws_services_and_runtime_gates() -> None:
    notifier = (ROOT / "functions/registration_notifier/aws_main.go").read_text()
    uploader = (ROOT / "functions/disk_uploader/aws_main.go").read_text()

    assert "dynamodb.NewFromConfig" in notifier
    assert "sqs.NewFromConfig" in notifier
    assert "ssm.NewFromConfig" in notifier
    assert 'os.Getenv("APPLICATION_MODE") == "active"' in notifier
    assert "s3.NewFromConfig" in uploader
    assert "upload-completions/" in uploader
    assert 'os.Getenv("APPLICATION_MODE") == "active"' in uploader


def test_phase_four_function_url_is_hmac_reachable_but_runtime_stays_gated() -> None:
    locals_config = (APPLICATION / "locals.tf").read_text()
    lambda_config = (APPLICATION / "lambda.tf").read_text()
    deploy = (ROOT / ".github/workflows/aws-deploy.yml").read_text()
    variables = (APPLICATION / "variables.tf").read_text()

    assert "vk_group_id" in locals_config
    assert 'authorization_type = "NONE"' in lambda_config
    assert 'function_url_auth_type = "NONE"' in lambda_config
    assert "gateway_function_url_invoke" in lambda_config
    assert "var.application_mode" in lambda_config
    assert "var.mutations_enabled" in lambda_config
    assert re.search(r'variable "application_mode"[^}]+default\s+= "standby"', variables, re.DOTALL)
    assert 'put_secret vk_group_id "${VK_GROUP_ID}"' in deploy
    assert "CF_TO_AWS_HMAC_SECRET" in deploy
