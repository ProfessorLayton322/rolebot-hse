resource "aws_sqs_queue" "registration_command_failures" {
  name                        = "${local.name_prefix}-registration-command-failures.fifo"
  fifo_queue                  = true
  content_based_deduplication = false
  message_retention_seconds   = 1209600
  sqs_managed_sse_enabled     = true

  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_sqs_queue" "registration_commands" {
  name                        = "${local.name_prefix}-registration-commands.fifo"
  fifo_queue                  = true
  content_based_deduplication = false
  visibility_timeout_seconds  = 180
  receive_wait_time_seconds   = 20
  message_retention_seconds   = 1209600
  max_message_size            = 262144
  sqs_managed_sse_enabled     = true

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.registration_command_failures.arn
    maxReceiveCount     = 5
  })

  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_sqs_queue_redrive_allow_policy" "registration_command_failures" {
  queue_url = aws_sqs_queue.registration_command_failures.id
  redrive_allow_policy = jsonencode({
    redrivePermission = "byQueue"
    sourceQueueArns   = [aws_sqs_queue.registration_commands.arn]
  })
}
