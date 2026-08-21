locals {
  function_environment = {
    YDB_ENDPOINT              = yandex_ydb_database_serverless.application.ydb_api_endpoint
    YDB_DATABASE              = yandex_ydb_database_serverless.application.database_path
    YMQ_ENDPOINT              = "https://message-queue.api.cloud.yandex.net"
    YMQ_FIFO_URL              = yandex_message_queue.registration_commands.id
    YMQ_KICK_URL              = yandex_message_queue.worker_kicks.id
    LOCKBOX_SECRET_ID         = yandex_lockbox_secret.application.id
    TELEGRAM_EGRESS_URL       = local.telegram_egress_url
    INLINE_SAFETY_MARGIN_MS   = "100"
    WORKBOOK_SCAN_CONCURRENCY = "3"
    APP_LOG_LEVEL             = "INFO"
  }
}

resource "yandex_function" "gateway" {
  name               = "${var.project_name}-gateway"
  description        = "Shared Telegram and VK application gateway"
  folder_id          = var.yandex_folder_id
  runtime            = "python312"
  entrypoint         = "main.handler"
  memory             = 512
  execution_timeout  = "30"
  concurrency        = 1
  service_account_id = yandex_iam_service_account.gateway.id
  environment        = local.function_environment
  tags               = ["production"]
  user_hash          = filesha256(var.function_zip_path)

  metadata_options {
    gce_http_endpoint    = 1
    aws_v1_http_endpoint = 2
  }

  content {
    zip_filename = var.function_zip_path
  }

  log_options {
    log_group_id = yandex_logging_group.application.id
    min_level    = "INFO"
  }
}

resource "yandex_function" "ordered_worker" {
  name               = "${var.project_name}-ordered-worker"
  description        = "Bounded FIFO drainer and XLSX mutation worker"
  folder_id          = var.yandex_folder_id
  runtime            = "python312"
  entrypoint         = "ordered_worker.handler"
  memory             = 1024
  execution_timeout  = "60"
  concurrency        = 1
  service_account_id = yandex_iam_service_account.worker.id
  environment = merge(local.function_environment, {
    WORKER_MAX_SECONDS = "40"
  })
  tags      = ["production"]
  user_hash = filesha256(var.function_zip_path)

  metadata_options {
    gce_http_endpoint    = 1
    aws_v1_http_endpoint = 2
  }

  content {
    zip_filename = var.function_zip_path
  }

  log_options {
    log_group_id = yandex_logging_group.application.id
    min_level    = "INFO"
  }
}

resource "yandex_function_scaling_policy" "gateway" {
  function_id = yandex_function.gateway.id
  policy {
    tag                  = "production"
    zone_instances_limit = var.gateway_max_instances
    zone_requests_limit  = 1
  }
}

resource "yandex_function_scaling_policy" "ordered_worker" {
  function_id = yandex_function.ordered_worker.id
  policy {
    tag                  = "production"
    zone_instances_limit = var.worker_max_instances
    zone_requests_limit  = 1
  }
}
