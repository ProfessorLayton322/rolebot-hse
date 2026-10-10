resource "aws_cloudwatch_log_group" "lambda" {
  for_each = local.function_names

  name              = "/aws/lambda/${each.value}"
  retention_in_days = 7
}

resource "aws_lambda_function" "application" {
  for_each = local.lambda_definitions

  function_name = local.function_names[each.key]
  description   = "Gated ${replace(each.key, "_", " ")} function; standby controls block production work"
  role          = aws_iam_role.lambda[each.key].arn
  runtime       = each.value.runtime
  handler       = each.value.handler
  architectures = ["x86_64"]
  filename      = each.value.package

  source_code_hash = filebase64sha256(each.value.package)
  memory_size      = each.value.memory_size
  timeout          = each.value.timeout

  environment {
    variables = {
      APP_LOG_LEVEL                = "INFO"
      APPLICATION_MODE             = "standby"
      MUTATIONS_ENABLED            = "false"
      CLOUD_PROVIDER               = "aws"
      AWS_STORAGE_BUCKET           = aws_s3_bucket.application.id
      AWS_FIFO_QUEUE_URL           = aws_sqs_queue.registration_commands.id
      AWS_PARAMETER_PREFIX         = "/${var.project_name}/production/"
      AWS_DISK_UPLOADER_FUNCTION   = local.function_names.disk_uploader
      DYNAMODB_TG_USERS_TABLE      = local.table_names.tg_users
      DYNAMODB_VK_USERS_TABLE      = local.table_names.vk_users
      DYNAMODB_EVENTS_TABLE        = local.table_names.events
      DYNAMODB_EVENT_LEADERS_TABLE = local.table_names.event_leaders
      DYNAMODB_REGISTRATIONS_TABLE = local.table_names.registrations
      TELEGRAM_EGRESS_URL          = "https://${var.project_name}-telegram-egress.${var.cloudflare_workers_subdomain}.workers.dev"
      GOOGLE_FOLDER_ID             = var.google_folder_id
      TG_ADMIN_IDS                 = var.tg_admin_ids
      VK_ADMIN_IDS                 = var.vk_admin_ids
      TG_GAMEMASTER_IDS            = var.tg_gamemaster_ids
      VK_GAMEMASTER_IDS            = var.vk_gamemaster_ids
    }
  }

  depends_on = [
    aws_cloudwatch_log_group.lambda,
    aws_iam_role_policy.lambda,
  ]
}

# Phase 1 creates a stable endpoint but keeps it private. Cloudflare cannot
# reach AWS until a later phase adds application HMAC verification and changes
# this auth mode deliberately.
resource "aws_lambda_function_url" "gateway" {
  function_name      = aws_lambda_function.application["gateway"].function_name
  authorization_type = "AWS_IAM"
  invoke_mode        = "BUFFERED"
}

resource "aws_lambda_event_source_mapping" "registration_commands" {
  event_source_arn                   = aws_sqs_queue.registration_commands.arn
  function_name                      = aws_lambda_function.application["ordered_worker"].arn
  enabled                            = false
  batch_size                         = 10
  maximum_batching_window_in_seconds = 0
  function_response_types            = ["ReportBatchItemFailures"]
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
