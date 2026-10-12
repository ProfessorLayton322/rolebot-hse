resource "aws_cloudwatch_log_group" "alarm_notifier" {
  name              = "/aws/lambda/${local.alarm_notifier_name}"
  retention_in_days = 7
}

resource "aws_iam_role" "alarm_notifier" {
  name               = local.alarm_notifier_name
  assume_role_policy = data.aws_iam_policy_document.lambda_assume_role.json
}

data "aws_iam_policy_document" "alarm_notifier" {
  statement {
    sid       = "WriteOwnLogs"
    effect    = "Allow"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.alarm_notifier.arn}:*"]
  }
}

resource "aws_iam_role_policy" "alarm_notifier" {
  name   = "${local.alarm_notifier_name}-runtime"
  role   = aws_iam_role.alarm_notifier.id
  policy = data.aws_iam_policy_document.alarm_notifier.json
}

resource "aws_lambda_function" "alarm_notifier" {
  function_name = local.alarm_notifier_name
  description   = "Publishes CloudWatch alarm and recovery transitions to ntfy"
  role          = aws_iam_role.alarm_notifier.arn
  runtime       = "python3.12"
  handler       = "aws_alarm_notifier.handler"
  architectures = ["x86_64"]
  filename      = var.alarm_notifier_zip_path

  source_code_hash = filebase64sha256(var.alarm_notifier_zip_path)
  memory_size      = 128
  timeout          = 15

  environment {
    variables = {
      NTFY_TOPIC = var.ntfy_topic
    }
  }

  depends_on = [aws_iam_role_policy.alarm_notifier]
}

resource "aws_lambda_permission" "cloudwatch_alarms" {
  statement_id   = "AllowCloudWatchAlarms"
  action         = "lambda:InvokeFunction"
  function_name  = aws_lambda_function.alarm_notifier.function_name
  principal      = "lambda.alarms.cloudwatch.amazonaws.com"
  source_account = var.aws_account_id
  source_arn     = "arn:aws:cloudwatch:${var.aws_region}:${var.aws_account_id}:alarm:${local.name_prefix}-*"
}
