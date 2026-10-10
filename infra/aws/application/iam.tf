data "aws_iam_policy_document" "lambda_assume_role" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "lambda" {
  for_each = local.function_names

  name               = each.value
  assume_role_policy = data.aws_iam_policy_document.lambda_assume_role.json
}

locals {
  dynamodb_table_arns = [
    aws_dynamodb_table.tg_users.arn,
    aws_dynamodb_table.vk_users.arn,
    aws_dynamodb_table.events.arn,
    aws_dynamodb_table.event_leaders.arn,
    aws_dynamodb_table.registrations.arn,
  ]
  dynamodb_index_arns = [for arn in local.dynamodb_table_arns : "${arn}/index/*"]
  dynamodb_resources  = concat(local.dynamodb_table_arns, local.dynamodb_index_arns)

  dynamodb_read_actions = [
    "dynamodb:BatchGetItem",
    "dynamodb:DescribeTable",
    "dynamodb:GetItem",
    "dynamodb:Query",
    "dynamodb:Scan",
    "dynamodb:TransactGetItems",
  ]
  dynamodb_write_actions = [
    "dynamodb:BatchWriteItem",
    "dynamodb:DeleteItem",
    "dynamodb:PutItem",
    "dynamodb:TransactWriteItems",
    "dynamodb:UpdateItem",
  ]
}

data "aws_iam_policy_document" "lambda" {
  for_each = local.function_names

  statement {
    sid       = "WriteOwnLogs"
    effect    = "Allow"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.lambda[each.key].arn}:*"]
  }

  dynamic "statement" {
    for_each = contains(["gateway", "ordered_worker", "registration_notifier", "upload_completion"], each.key) ? [1] : []
    content {
      sid       = "ReadApplicationTables"
      effect    = "Allow"
      actions   = local.dynamodb_read_actions
      resources = local.dynamodb_resources
    }
  }

  dynamic "statement" {
    for_each = contains(["gateway", "ordered_worker", "registration_notifier", "upload_completion"], each.key) ? [1] : []
    content {
      sid       = "WriteApplicationTables"
      effect    = "Allow"
      actions   = local.dynamodb_write_actions
      resources = local.dynamodb_resources
    }
  }

  dynamic "statement" {
    for_each = contains(["gateway", "registration_notifier"], each.key) ? [1] : []
    content {
      sid       = "PublishRegistrationCommands"
      effect    = "Allow"
      actions   = ["sqs:GetQueueAttributes", "sqs:GetQueueUrl", "sqs:SendMessage"]
      resources = [aws_sqs_queue.registration_commands.arn]
    }
  }

  dynamic "statement" {
    for_each = each.key == "ordered_worker" ? [1] : []
    content {
      sid    = "ConsumeRegistrationCommands"
      effect = "Allow"
      actions = [
        "sqs:ChangeMessageVisibility",
        "sqs:DeleteMessage",
        "sqs:GetQueueAttributes",
        "sqs:ReceiveMessage",
      ]
      resources = [aws_sqs_queue.registration_commands.arn]
    }
  }

  dynamic "statement" {
    for_each = contains(["gateway", "ordered_worker", "disk_uploader", "upload_completion"], each.key) ? [1] : []
    content {
      sid    = "UseApplicationStorage"
      effect = "Allow"
      actions = [
        "s3:DeleteObject",
        "s3:GetObject",
        "s3:GetObjectVersion",
        "s3:PutObject",
      ]
      resources = ["${aws_s3_bucket.application.arn}/*"]
    }
  }

  dynamic "statement" {
    for_each = contains(["gateway", "ordered_worker"], each.key) ? [1] : []
    content {
      sid       = "InvokeDiskUploader"
      effect    = "Allow"
      actions   = ["lambda:InvokeFunction"]
      resources = ["arn:aws:lambda:${var.aws_region}:${var.aws_account_id}:function:${local.function_names.disk_uploader}"]
    }
  }

  statement {
    sid     = "ReadRuntimeParameters"
    effect  = "Allow"
    actions = ["ssm:GetParameter", "ssm:GetParameters"]
    resources = [
      for key in local.lambda_parameter_keys[each.key] : local.parameter_arns[key]
    ]
  }
}

resource "aws_iam_role_policy" "lambda" {
  for_each = local.function_names

  name   = "${each.value}-runtime"
  role   = aws_iam_role.lambda[each.key].id
  policy = data.aws_iam_policy_document.lambda[each.key].json
}

data "aws_iam_policy_document" "scheduler_assume_role" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["scheduler.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "scheduler" {
  name               = "${local.name_prefix}-scheduler"
  assume_role_policy = data.aws_iam_policy_document.scheduler_assume_role.json
}

data "aws_iam_policy_document" "scheduler" {
  statement {
    effect    = "Allow"
    actions   = ["lambda:InvokeFunction"]
    resources = [aws_lambda_function.application["registration_notifier"].arn]
  }
}

resource "aws_iam_role_policy" "scheduler" {
  name   = "${local.name_prefix}-invoke-notifier"
  role   = aws_iam_role.scheduler.id
  policy = data.aws_iam_policy_document.scheduler.json
}
