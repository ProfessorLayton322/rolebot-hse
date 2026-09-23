locals {
  function_package_hash      = filesha256(var.function_zip_path)
  disk_uploader_package_hash = filesha256(var.disk_uploader_zip_path)
  runtime_config_url         = "${local.telegram_egress_url}/runtime/config"

  base_function_environment = {
    # The Python YDB driver requires the TLS scheme. ydb_api_endpoint contains
    # only host:port; passing it through unchanged makes every driver wait time
    # out before a real bot update can be handled.
    YDB_ENDPOINT            = "grpcs://${yandex_ydb_database_serverless.application.ydb_api_endpoint}"
    YDB_DATABASE            = yandex_ydb_database_serverless.application.database_path
    YMQ_ENDPOINT            = "https://message-queue.api.cloud.yandex.net"
    YMQ_FIFO_URL            = yandex_message_queue.registration_commands.id
    YMQ_KICK_URL            = yandex_message_queue.worker_kicks.id
    RUNTIME_CONFIG_URL      = local.runtime_config_url
    RUNTIME_CONFIG_AUDIENCE = local.runtime_config_url
    TELEGRAM_EGRESS_URL     = local.telegram_egress_url
    GOOGLE_FOLDER_ID        = var.google_folder_id
    INLINE_SAFETY_MARGIN_MS = "100"
    APP_LOG_LEVEL           = "INFO"
  }

  function_environment = merge(local.base_function_environment, {
    DISK_STAGING_BUCKET = yandex_storage_bucket.deployment.bucket
    DISK_UPLOADER_URL   = "https://functions.yandexcloud.net/${yandex_function.disk_uploader.id}"
  })
}

# Function packages above the inline API limit must be uploaded through Object
# Storage. A stable object key plus source_hash updates the object in place,
# while user_hash makes Terraform create new Function versions when it changes.
resource "yandex_storage_object" "function_package" {
  bucket       = yandex_storage_bucket.deployment.bucket
  key          = "${var.project_name}/functions/function-package.zip"
  source       = var.function_zip_path
  source_hash  = local.function_package_hash
  content_type = "application/zip"
}

resource "yandex_storage_object" "disk_uploader_package" {
  bucket       = yandex_storage_bucket.deployment.bucket
  key          = "${var.project_name}/functions/disk-uploader.zip"
  source       = var.disk_uploader_zip_path
  source_hash  = local.disk_uploader_package_hash
  content_type = "application/zip"
}

resource "yandex_function" "disk_uploader" {
  name               = "${var.project_name}-disk-uploader"
  description        = "Stream staged files from Object Storage to Yandex Disk"
  folder_id          = var.yandex_folder_id
  runtime            = "golang123"
  entrypoint         = "index.Handler"
  memory             = 128
  execution_timeout  = "300"
  concurrency        = 1
  service_account_id = yandex_iam_service_account.disk_uploader.id
  environment = {
    DISK_STAGING_BUCKET        = yandex_storage_bucket.deployment.bucket
    RUNTIME_CONFIG_URL         = local.runtime_config_url
    RUNTIME_CONFIG_AUDIENCE    = local.runtime_config_url
    RUNTIME_SERVICE_ACCOUNT_ID = yandex_iam_service_account.disk_uploader.id
    UPLOAD_COMPLETION_URL      = "https://functions.yandexcloud.net/${yandex_function.upload_completion.id}"
  }
  tags      = ["production"]
  user_hash = local.disk_uploader_package_hash

  metadata_options {
    gce_http_endpoint    = 1
    aws_v1_http_endpoint = 2
  }

  package {
    bucket_name = yandex_storage_object.disk_uploader_package.bucket
    object_name = yandex_storage_object.disk_uploader_package.key
    sha_256     = local.disk_uploader_package_hash
  }

  async_invocation {
    retries_count      = 2
    service_account_id = yandex_iam_service_account.trigger.id
  }

  log_options {
    log_group_id = yandex_logging_group.application.id
    min_level    = "INFO"
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
  environment = merge(local.function_environment, {
    RUNTIME_SERVICE_ACCOUNT_ID = yandex_iam_service_account.gateway.id
  })
  tags      = ["production"]
  user_hash = local.function_package_hash

  metadata_options {
    gce_http_endpoint    = 1
    aws_v1_http_endpoint = 2
  }

  package {
    bucket_name = yandex_storage_object.function_package.bucket
    object_name = yandex_storage_object.function_package.key
    sha_256     = local.function_package_hash
  }

  log_options {
    log_group_id = yandex_logging_group.application.id
    min_level    = "INFO"
  }

  depends_on = [
    yandex_ydb_table.tg_users,
    yandex_ydb_table.vk_users,
    yandex_ydb_table.events,
    yandex_ydb_table.registrations,
  ]
}

resource "yandex_function" "ordered_worker" {
  name               = "${var.project_name}-ordered-worker"
  description        = "Bounded FIFO drainer, YDB registration writer, and XLSX projector"
  folder_id          = var.yandex_folder_id
  runtime            = "python312"
  entrypoint         = "ordered_worker.handler"
  memory             = 1024
  execution_timeout  = "60"
  concurrency        = 1
  service_account_id = yandex_iam_service_account.worker.id
  environment = merge(local.function_environment, {
    RUNTIME_SERVICE_ACCOUNT_ID = yandex_iam_service_account.worker.id
    WORKER_MAX_SECONDS         = "40"
  })
  tags      = ["production"]
  user_hash = local.function_package_hash

  metadata_options {
    gce_http_endpoint    = 1
    aws_v1_http_endpoint = 2
  }

  package {
    bucket_name = yandex_storage_object.function_package.bucket
    object_name = yandex_storage_object.function_package.key
    sha_256     = local.function_package_hash
  }

  log_options {
    log_group_id = yandex_logging_group.application.id
    min_level    = "INFO"
  }

  depends_on = [
    yandex_ydb_table.tg_users,
    yandex_ydb_table.vk_users,
    yandex_ydb_table.events,
    yandex_ydb_table.registrations,
  ]
}

resource "yandex_function" "registration_notifier" {
  name               = "${var.project_name}-registration-notifier"
  description        = "Notify admins and moderators after scheduled registration opens"
  folder_id          = var.yandex_folder_id
  runtime            = "python312"
  entrypoint         = "registration_notifier.handler"
  memory             = 512
  execution_timeout  = "60"
  concurrency        = 1
  service_account_id = yandex_iam_service_account.worker.id
  environment = merge(local.function_environment, {
    RUNTIME_SERVICE_ACCOUNT_ID = yandex_iam_service_account.worker.id
  })
  tags      = ["production"]
  user_hash = local.function_package_hash

  metadata_options {
    gce_http_endpoint    = 1
    aws_v1_http_endpoint = 2
  }

  package {
    bucket_name = yandex_storage_object.function_package.bucket
    object_name = yandex_storage_object.function_package.key
    sha_256     = local.function_package_hash
  }

  log_options {
    log_group_id = yandex_logging_group.application.id
    min_level    = "INFO"
  }

  depends_on = [yandex_ydb_table.events]
}

resource "yandex_function" "upload_completion" {
  name               = "${var.project_name}-upload-completion"
  description        = "Persist completed Yandex Disk links and notify their requester"
  folder_id          = var.yandex_folder_id
  runtime            = "python312"
  entrypoint         = "upload_completion.handler"
  memory             = 512
  execution_timeout  = "30"
  concurrency        = 1
  service_account_id = yandex_iam_service_account.worker.id
  environment = merge(local.base_function_environment, {
    RUNTIME_SERVICE_ACCOUNT_ID = yandex_iam_service_account.worker.id
    DISK_STAGING_BUCKET        = yandex_storage_bucket.deployment.bucket
    # The completion handler never submits uploads; this non-routable value
    # keeps the shared container configuration cycle-free.
    DISK_UPLOADER_URL = "https://unused.invalid"
  })
  tags      = ["production"]
  user_hash = local.function_package_hash

  metadata_options {
    gce_http_endpoint    = 1
    aws_v1_http_endpoint = 2
  }

  package {
    bucket_name = yandex_storage_object.function_package.bucket
    object_name = yandex_storage_object.function_package.key
    sha_256     = local.function_package_hash
  }

  async_invocation {
    retries_count      = 2
    service_account_id = yandex_iam_service_account.trigger.id
  }

  log_options {
    log_group_id = yandex_logging_group.application.id
    min_level    = "INFO"
  }

  depends_on = [
    yandex_ydb_table.tg_users,
    yandex_ydb_table.vk_users,
    yandex_ydb_table.events,
  ]
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

resource "yandex_function_scaling_policy" "registration_notifier" {
  function_id = yandex_function.registration_notifier.id
  policy {
    tag                  = "production"
    zone_instances_limit = 1
    zone_requests_limit  = 1
  }
}

resource "yandex_function_scaling_policy" "disk_uploader" {
  function_id = yandex_function.disk_uploader.id
  policy {
    tag                  = "production"
    zone_instances_limit = 1
    zone_requests_limit  = 1
  }
}

resource "yandex_function_scaling_policy" "upload_completion" {
  function_id = yandex_function.upload_completion.id
  policy {
    tag                  = "production"
    zone_instances_limit = 1
    zone_requests_limit  = 1
  }
}
