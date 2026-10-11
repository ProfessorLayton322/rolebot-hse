resource "aws_cloudwatch_log_group" "lambda" {
  for_each = local.function_names

  name              = "/aws/lambda/${each.value}"
  retention_in_days = 7
}

resource "aws_lambda_function" "application" {
  for_each = local.lambda_definitions

  function_name = local.function_names[each.key]
  description   = "Gated ${replace(each.key, "_", " ")} function; runtime controls protect cutover and rollback"
  role          = aws_iam_role.lambda[each.key].arn
  runtime       = each.value.runtime
  handler       = each.value.handler
  architectures = ["x86_64"]
  filename      = each.value.package

  source_code_hash = filebase64sha256(each.value.package)
  memory_size      = each.value.memory_size
  timeout          = each.value.timeout
  # The reversible DynamoDB adapter intentionally uses whole-table snapshots
  # for a few projections. Keep queue batches sequential so independent FIFO
  # groups cannot stampede the free-tier provisioned read capacity.
  reserved_concurrent_executions = each.key == "ordered_worker" ? 1 : -1

  environment {
    variables = {
      APP_LOG_LEVEL = "INFO"
      APPLICATION_MODE = (
        each.key == "registration_notifier" && !var.scheduler_enabled ? "standby" : var.application_mode
      )
      MUTATIONS_ENABLED = tostring(
        each.key == "registration_notifier" && !var.scheduler_enabled ? false : var.mutations_enabled
      )
      CLOUD_PROVIDER               = "aws"
      AWS_STORAGE_BUCKET           = aws_s3_bucket.application.id
      AWS_FIFO_QUEUE_URL           = aws_sqs_queue.registration_commands.id
      AWS_PARAMETER_PREFIX         = "/${var.project_name}/production/"
      AWS_DISK_UPLOADER_FUNCTION   = local.function_names.disk_uploader
      DYNAMODB_TG_USERS_TABLE      = local.runtime_table_names.tg_users
      DYNAMODB_VK_USERS_TABLE      = local.runtime_table_names.vk_users
      DYNAMODB_EVENTS_TABLE        = local.runtime_table_names.events
      DYNAMODB_EVENT_LEADERS_TABLE = local.runtime_table_names.event_leaders
      DYNAMODB_REGISTRATIONS_TABLE = local.runtime_table_names.registrations
      TELEGRAM_EGRESS_URL          = "https://${var.project_name}-telegram-egress.${var.cloudflare_workers_subdomain}.workers.dev"
      GOOGLE_FOLDER_ID             = var.google_folder_id
      GOOGLE_APPS_SCRIPT_URL       = var.google_apps_script_url
      TG_ADMIN_IDS                 = var.runtime_table_target == "rehearsal" ? "[]" : var.tg_admin_ids
      VK_ADMIN_IDS                 = var.runtime_table_target == "rehearsal" ? "[]" : var.vk_admin_ids
      TG_GAMEMASTER_IDS            = var.runtime_table_target == "rehearsal" ? "[]" : var.tg_gamemaster_ids
      VK_GAMEMASTER_IDS            = var.runtime_table_target == "rehearsal" ? "[]" : var.vk_gamemaster_ids
    }
  }

  depends_on = [
    aws_cloudwatch_log_group.lambda,
    aws_iam_role_policy.lambda,
  ]
}

# The URL is intentionally public at the AWS edge so Cloudflare can reach it.
# Both accepted routes verify the backend-specific HMAC before application work,
# while the fixed standby runtime gate continues to reject production traffic.
resource "aws_lambda_function_url" "gateway" {
  function_name      = aws_lambda_function.application["gateway"].function_name
  authorization_type = "NONE"
  invoke_mode        = "BUFFERED"
}

resource "aws_lambda_permission" "gateway_function_url" {
  statement_id           = "AllowCloudflareFunctionUrl"
  action                 = "lambda:InvokeFunctionUrl"
  function_name          = aws_lambda_function.application["gateway"].function_name
  principal              = "*"
  function_url_auth_type = "NONE"
}

# AWS requires both permissions for newly created public Function URLs. This
# second statement remains restricted to invocations arriving through the URL.
resource "aws_lambda_permission" "gateway_function_url_invoke" {
  statement_id             = "AllowCloudflareFunctionUrlInvoke"
  action                   = "lambda:InvokeFunction"
  function_name            = aws_lambda_function.application["gateway"].function_name
  principal                = "*"
  invoked_via_function_url = true
}

resource "aws_lambda_event_source_mapping" "registration_commands" {
  event_source_arn                   = aws_sqs_queue.registration_commands.arn
  function_name                      = aws_lambda_function.application["ordered_worker"].arn
  enabled                            = var.sqs_event_mapping_enabled
  # A command can spend most of the worker's application deadline rebuilding
  # player and pass tables. Give every command its own invocation so a batch of
  # expensive commands cannot time out and defer otherwise successful work.
  batch_size                         = 1
  maximum_batching_window_in_seconds = 0
  function_response_types            = ["ReportBatchItemFailures"]

  metrics_config {
    metrics = ["EventCount"]
  }
}

resource "aws_lambda_permission" "s3_upload_completion" {
  statement_id   = "AllowApplicationBucket"
  action         = "lambda:InvokeFunction"
  function_name  = aws_lambda_function.application["upload_completion"].function_name
  principal      = "s3.amazonaws.com"
  source_arn     = aws_s3_bucket.application.arn
  source_account = var.aws_account_id
}

resource "aws_s3_bucket_notification" "upload_completion" {
  bucket = aws_s3_bucket.application.id

  lambda_function {
    lambda_function_arn = aws_lambda_function.application["upload_completion"].arn
    events              = ["s3:ObjectCreated:*"]
    filter_prefix       = "upload-completions/"
    filter_suffix       = ".json"
  }

  depends_on = [aws_lambda_permission.s3_upload_completion]
}
