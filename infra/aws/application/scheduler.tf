resource "aws_scheduler_schedule" "registration_notifications" {
  name        = "${local.name_prefix}-registration-notifications"
  description = "Gated hourly registration lifecycle notifier"
  state       = var.scheduler_enabled ? "ENABLED" : "DISABLED"

  schedule_expression          = "cron(0 * * * ? *)"
  schedule_expression_timezone = "UTC"

  flexible_time_window {
    mode = "OFF"
  }

  target {
    arn      = aws_lambda_function.application["registration_notifier"].arn
    role_arn = aws_iam_role.scheduler.arn
    input    = jsonencode({ source = "eventbridge-scheduler" })

    retry_policy {
      maximum_event_age_in_seconds = 3600
      maximum_retry_attempts       = 2
    }
  }

  depends_on = [aws_iam_role_policy.scheduler]
}
