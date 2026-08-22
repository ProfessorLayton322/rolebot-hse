resource "yandex_iam_service_account" "gateway" {
  name        = "${var.project_name}-gateway"
  description = "Runtime identity for the shared Telegram/VK gateway"
  folder_id   = var.yandex_folder_id
}

resource "yandex_iam_service_account" "worker" {
  name        = "${var.project_name}-ordered-worker"
  description = "Runtime identity for the FIFO workbook drainer"
  folder_id   = var.yandex_folder_id
}

resource "yandex_iam_service_account" "trigger" {
  name        = "${var.project_name}-kick-trigger"
  description = "Reads only the standard kick queue and invokes the worker"
  folder_id   = var.yandex_folder_id
}

resource "yandex_iam_service_account" "api_gateway" {
  name        = "${var.project_name}-api-gateway"
  description = "Invokes only the gateway Function"
  folder_id   = var.yandex_folder_id
}

resource "yandex_iam_service_account" "ymq_client" {
  name        = "${var.project_name}-ymq-client"
  description = "Static-key identity used solely for Message Queue API calls"
  folder_id   = var.yandex_folder_id
}

# YMQ's SQS-compatible Terraform/API authentication requires a static access key.
# The secret is copied into a Cloudflare Worker secret binding by CI and never
# placed in a Function environment variable.
resource "yandex_iam_service_account_static_access_key" "ymq_client" {
  service_account_id = yandex_iam_service_account.ymq_client.id
  description        = "${var.project_name} YMQ client; rotate through Terraform"
}

locals {
  runtime_bindings = {
    gateway_ydb    = { role = "ydb.editor", member = yandex_iam_service_account.gateway.id }
    worker_ydb     = { role = "ydb.editor", member = yandex_iam_service_account.worker.id }
    ymq_reader     = { role = "ymq.reader", member = yandex_iam_service_account.ymq_client.id }
    ymq_writer     = { role = "ymq.writer", member = yandex_iam_service_account.ymq_client.id }
    trigger_reader = { role = "ymq.reader", member = yandex_iam_service_account.trigger.id }
  }
}

# Runtime Functions exchange their platform-provided IAM tokens for one-hour
# Yandex OIDC ID tokens. Each identity can mint a token only for itself; the
# Cloudflare Worker verifies the signature, audience, expiry, and subject.
resource "yandex_iam_service_account_iam_member" "runtime_self_token_creator" {
  for_each = {
    gateway = yandex_iam_service_account.gateway.id
    worker  = yandex_iam_service_account.worker.id
  }

  service_account_id = each.value
  role               = "iam.serviceAccounts.tokenCreator"
  member             = "serviceAccount:${each.value}"
}

resource "yandex_resourcemanager_folder_iam_member" "runtime" {
  for_each  = local.runtime_bindings
  folder_id = var.yandex_folder_id
  role      = each.value.role
  member    = "serviceAccount:${each.value.member}"
}

# IAM bindings are eventually consistent. Queue creation authenticates with
# the newly issued ymq_client static key, so wait until its writer permission
# has had time to propagate before making the first SQS-compatible API call.
resource "time_sleep" "ymq_writer_ready" {
  depends_on      = [yandex_resourcemanager_folder_iam_member.runtime["ymq_writer"]]
  create_duration = "60s"

  triggers = {
    binding_id = yandex_resourcemanager_folder_iam_member.runtime["ymq_writer"].id
  }
}

resource "yandex_function_iam_member" "gateway_from_api_gateway" {
  function_id = yandex_function.gateway.id
  role        = "functions.functionInvoker"
  member      = "serviceAccount:${yandex_iam_service_account.api_gateway.id}"
}

resource "yandex_function_iam_member" "worker_from_trigger" {
  function_id = yandex_function.ordered_worker.id
  role        = "functions.functionInvoker"
  member      = "serviceAccount:${yandex_iam_service_account.trigger.id}"
}
