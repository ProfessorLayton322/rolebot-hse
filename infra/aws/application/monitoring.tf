resource "aws_cloudwatch_metric_alarm" "registration_dlq_not_empty" {
  alarm_name          = "${local.name_prefix}-registration-dlq-not-empty"
  alarm_description   = "Registration commands reached the FIFO dead-letter queue."
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "ApproximateNumberOfMessagesVisible"
  namespace           = "AWS/SQS"
  period              = 300
  statistic           = "Maximum"
  threshold           = 0
  treat_missing_data  = "notBreaching"

  dimensions = {
    QueueName = aws_sqs_queue.registration_command_failures.name
  }
}

resource "aws_cloudwatch_metric_alarm" "registration_queue_age" {
  alarm_name          = "${local.name_prefix}-registration-queue-age"
  alarm_description   = "The oldest registration command has waited more than five minutes."
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 2
  metric_name         = "ApproximateAgeOfOldestMessage"
  namespace           = "AWS/SQS"
  period              = 300
  statistic           = "Maximum"
  threshold           = 300
  treat_missing_data  = "notBreaching"

  dimensions = {
    QueueName = aws_sqs_queue.registration_commands.name
  }
}
