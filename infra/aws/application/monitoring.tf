locals {
  dashboard_period_seconds = 300
  monitoring_function_names = merge(local.function_names, {
    alarm_notifier = local.alarm_notifier_name
  })
  alarm_notification_actions = [
    aws_lambda_function.alarm_notifier.arn,
  ]

  lambda_failure_signals = {
    invocation_errors = {
      id          = "err"
      metric_name = "Errors"
      label       = "Invocation errors"
      description = "One or more Lambda invocations failed."
    }
    throttles = {
      id          = "thr"
      metric_name = "Throttles"
      label       = "Throttles"
      description = "One or more Lambda invocation requests were throttled."
    }
    dead_letter_errors = {
      id          = "dle"
      metric_name = "DeadLetterErrors"
      label       = "Dead-letter delivery errors"
      description = "Lambda could not deliver one or more asynchronous events to a dead-letter queue."
    }
    destination_delivery_failures = {
      id          = "ddf"
      metric_name = "DestinationDeliveryFailures"
      label       = "Destination delivery failures"
      description = "Lambda could not deliver one or more records to a configured destination."
    }
    async_events_dropped = {
      id          = "aed"
      metric_name = "AsyncEventsDropped"
      label       = "Async events dropped"
      description = "Lambda dropped one or more asynchronous events after retries or expiry."
    }
    recursive_invocations_dropped = {
      id          = "rid"
      metric_name = "RecursiveInvocationsDropped"
      label       = "Recursive invocations dropped"
      description = "Lambda stopped one or more invocations after detecting a recursive loop."
    }
  }

  lambda_function_index = {
    for index, key in sort(keys(local.monitoring_function_names)) : tostring(index) => {
      key  = key
      name = local.monitoring_function_names[key]
    }
  }

  lambda_compute_rate_metrics = concat([
    for key, definition in local.lambda_definitions : [
      [
        "AWS/Lambda",
        "Duration",
        "FunctionName",
        local.monitoring_function_names[key],
        {
          id      = "duration_${key}"
          label   = "${replace(key, "_", " ")} duration"
          period  = local.dashboard_period_seconds
          stat    = "Sum"
          visible = false
        },
      ],
      [
        {
          expression = "duration_${key} * ${definition.memory_size} / 1024 / 1000 / ${local.dashboard_period_seconds}"
          id         = "gbs_${key}"
          label      = replace(key, "_", " ")
        },
      ],
    ]
  ]...)

  lambda_error_rate_metrics = concat([
    for index, key in sort(keys(local.monitoring_function_names)) : [
      [
        "AWS/Lambda",
        "Invocations",
        "FunctionName",
        local.monitoring_function_names[key],
        {
          id      = "inv_${index}"
          period  = local.dashboard_period_seconds
          stat    = "Sum"
          visible = false
        },
      ],
      [
        "AWS/Lambda",
        "Errors",
        "FunctionName",
        local.monitoring_function_names[key],
        {
          id      = "errors_${index}"
          period  = local.dashboard_period_seconds
          stat    = "Sum"
          visible = false
        },
      ],
      [
        {
          expression = "IF(inv_${index} > 0, 100 * FILL(errors_${index}, 0) / inv_${index}, 0)"
          id         = "error_rate_${index}"
          label      = replace(key, "_", " ")
        },
      ],
    ]
  ]...)

  lambda_failure_count_metrics = concat([
    for signal_key, signal in local.lambda_failure_signals : concat(
      [
        for index, key in sort(keys(local.monitoring_function_names)) : [
          "AWS/Lambda",
          signal.metric_name,
          "FunctionName",
          local.monitoring_function_names[key],
          {
            id      = "${signal.id}_${index}"
            period  = local.dashboard_period_seconds
            stat    = "Sum"
            visible = false
          },
        ]
      ],
      [[
        {
          expression = "SUM([${join(",", [for index, _ in sort(keys(local.monitoring_function_names)) : "${signal.id}_${index}"])}])"
          id         = "total_${signal.id}"
          label      = signal.label
        },
      ]],
    )
  ]...)

  lambda_async_age_metrics = [
    for key, name in local.monitoring_function_names : [
      "AWS/Lambda",
      "AsyncEventAge",
      "FunctionName",
      name,
      {
        label  = replace(key, "_", " ")
        period = local.dashboard_period_seconds
        stat   = "Maximum"
      },
    ]
  ]

  dynamodb_table_filter = join(" OR ", [
    for name in values(local.runtime_table_names) : "TableName = '${name}'"
  ])

  dynamodb_retry_metrics = concat([
    for key, name in local.runtime_table_names : [
      [
        "AWS/DynamoDB",
        "ReadThrottleEvents",
        "TableName",
        name,
        {
          label  = "${replace(key, "_", " ")} read throttles"
          period = local.dashboard_period_seconds
          stat   = "Sum"
        },
      ],
      [
        "AWS/DynamoDB",
        "WriteThrottleEvents",
        "TableName",
        name,
        {
          label  = "${replace(key, "_", " ")} write throttles"
          period = local.dashboard_period_seconds
          stat   = "Sum"
        },
      ],
      [
        "AWS/DynamoDB",
        "TransactionConflict",
        "TableName",
        name,
        {
          label  = "${replace(key, "_", " ")} transaction conflicts"
          period = local.dashboard_period_seconds
          stat   = "Sum"
        },
      ],
    ]
  ]...)
}

check "dashboard_generated_metrics_are_rows" {
  assert {
    condition = alltrue(concat(
      [for row in local.lambda_compute_rate_metrics : can(row[0])],
      [for row in local.lambda_error_rate_metrics : can(row[0])],
      [for row in local.lambda_failure_count_metrics : can(row[0])],
      [for row in local.dynamodb_retry_metrics : can(row[0])],
    ))
    error_message = "Every generated CloudWatch metric entry must remain an array row. Do not recursively flatten dashboard metrics."
  }
}

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
  alarm_actions       = local.alarm_notification_actions
  ok_actions          = local.alarm_notification_actions

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
  alarm_actions       = local.alarm_notification_actions
  ok_actions          = local.alarm_notification_actions

  dimensions = {
    QueueName = aws_sqs_queue.registration_commands.name
  }
}

resource "aws_cloudwatch_metric_alarm" "lambda_failure" {
  for_each = local.lambda_failure_signals

  alarm_name          = "${local.name_prefix}-lambda-${each.key}"
  alarm_description   = each.value.description
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  threshold           = 0
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.alarm_notification_actions
  ok_actions          = local.alarm_notification_actions

  metric_query {
    id          = "total"
    expression  = "SUM([${join(",", [for index, _ in local.lambda_function_index : "m${index}"])}])"
    label       = each.value.label
    return_data = true
  }

  dynamic "metric_query" {
    for_each = local.lambda_function_index

    content {
      id          = "m${metric_query.key}"
      return_data = false

      metric {
        dimensions = {
          FunctionName = metric_query.value.name
        }
        metric_name = each.value.metric_name
        namespace   = "AWS/Lambda"
        period      = local.dashboard_period_seconds
        stat        = "Sum"
      }
    }
  }
}

resource "aws_cloudwatch_metric_alarm" "lambda_async_event_age" {
  alarm_name          = "${local.name_prefix}-lambda-async-event-age"
  alarm_description   = "An asynchronous Lambda event has waited more than five minutes, commonly because of retries or throttling."
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 2
  datapoints_to_alarm = 2
  threshold           = 300000
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.alarm_notification_actions
  ok_actions          = local.alarm_notification_actions

  metric_query {
    id          = "oldest"
    expression  = "MAX([${join(",", [for index, _ in local.lambda_function_index : "m${index}"])}])"
    label       = "Oldest async event"
    return_data = true
  }

  dynamic "metric_query" {
    for_each = local.lambda_function_index

    content {
      id          = "m${metric_query.key}"
      return_data = false

      metric {
        dimensions = {
          FunctionName = metric_query.value.name
        }
        metric_name = "AsyncEventAge"
        namespace   = "AWS/Lambda"
        period      = local.dashboard_period_seconds
        stat        = "Maximum"
      }
    }
  }
}

resource "aws_cloudwatch_metric_alarm" "gateway_url_server_errors" {
  alarm_name          = "${local.name_prefix}-gateway-url-5xx"
  alarm_description   = "The public gateway Function URL returned one or more 5xx responses."
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "Url5xxCount"
  namespace           = "AWS/Lambda"
  period              = local.dashboard_period_seconds
  statistic           = "Sum"
  threshold           = 0
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.alarm_notification_actions
  ok_actions          = local.alarm_notification_actions

  dimensions = {
    FunctionName = local.function_names.gateway
  }
}

resource "aws_cloudwatch_metric_alarm" "registration_event_source_failures" {
  alarm_name          = "${local.name_prefix}-registration-event-source-failures"
  alarm_description   = "The SQS event source mapping failed to invoke the ordered worker for one or more events."
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "FailedInvokeEventCount"
  namespace           = "AWS/Lambda"
  period              = local.dashboard_period_seconds
  statistic           = "Sum"
  threshold           = 0
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.alarm_notification_actions
  ok_actions          = local.alarm_notification_actions

  dimensions = {
    EventSourceMappingUUID = aws_lambda_event_source_mapping.registration_commands.uuid
  }
}

resource "aws_cloudwatch_metric_alarm" "registration_retry_storm" {
  alarm_name          = "${local.name_prefix}-registration-retry-storm"
  alarm_description   = "For two consecutive five-minute periods, at least five ordered events were invoked at more than twice the successful-delete rate."
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 2
  datapoints_to_alarm = 2
  threshold           = 2
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.alarm_notification_actions
  ok_actions          = local.alarm_notification_actions

  metric_query {
    id          = "amplification"
    expression  = "IF(invoked >= 5, invoked / IF(deleted > 0, deleted, 1), 0)"
    label       = "Retry amplification"
    return_data = true
  }

  metric_query {
    id          = "invoked"
    return_data = false

    metric {
      dimensions = {
        EventSourceMappingUUID = aws_lambda_event_source_mapping.registration_commands.uuid
      }
      metric_name = "InvokedEventCount"
      namespace   = "AWS/Lambda"
      period      = local.dashboard_period_seconds
      stat        = "Sum"
    }
  }

  metric_query {
    id          = "deleted"
    return_data = false

    metric {
      dimensions = {
        EventSourceMappingUUID = aws_lambda_event_source_mapping.registration_commands.uuid
      }
      metric_name = "DeletedEventCount"
      namespace   = "AWS/Lambda"
      period      = local.dashboard_period_seconds
      stat        = "Sum"
    }
  }
}

resource "aws_cloudwatch_metric_alarm" "scheduler_delivery_failures" {
  alarm_name          = "${local.name_prefix}-scheduler-delivery-failures"
  alarm_description   = "EventBridge Scheduler failed, throttled, or exhausted retries while delivering a lifecycle invocation."
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  threshold           = 0
  treat_missing_data  = "notBreaching"
  alarm_actions       = local.alarm_notification_actions
  ok_actions          = local.alarm_notification_actions

  metric_query {
    id          = "total"
    expression  = "SUM([target_errors,target_throttles,invocation_throttles,dropped])"
    label       = "Scheduler delivery failures"
    return_data = true
  }

  dynamic "metric_query" {
    for_each = {
      target_errors        = "TargetErrorCount"
      target_throttles     = "TargetErrorThrottledCount"
      invocation_throttles = "InvocationThrottleCount"
      dropped              = "InvocationDroppedCount"
    }

    content {
      id          = metric_query.key
      return_data = false

      metric {
        dimensions = {
          ScheduleGroup = "default"
        }
        metric_name = metric_query.value
        namespace   = "AWS/Scheduler"
        period      = local.dashboard_period_seconds
        stat        = "Sum"
      }
    }
  }
}

resource "aws_cloudwatch_dashboard" "operations" {
  dashboard_name = "${local.name_prefix}-operations"

  dashboard_body = jsonencode({
    start          = "-PT6H"
    periodOverride = "inherit"
    widgets = [
      {
        type   = "text"
        x      = 0
        y      = 0
        width  = 24
        height = 3
        properties = {
          markdown = <<-EOT
            # ${local.name_prefix} operations
            **GB-s/s** is an approximate Lambda compute burn rate: configured memory × `Duration`, divided by each 5-minute period. It excludes requests, logs, storage, transfer, free-tier credits, and duration-rounding differences. **Retry amplification above 1** means events are being invoked more often than they are successfully deleted. The log query scans the selected time range when this dashboard is viewed.
          EOT
        }
      },
      {
        type   = "alarm"
        x      = 0
        y      = 3
        width  = 24
        height = 5
        properties = {
          title  = "Operational alarms"
          sortBy = "stateUpdatedTimestamp"
          alarms = concat(
            [
              aws_cloudwatch_metric_alarm.registration_dlq_not_empty.arn,
              aws_cloudwatch_metric_alarm.registration_queue_age.arn,
              aws_cloudwatch_metric_alarm.lambda_async_event_age.arn,
              aws_cloudwatch_metric_alarm.gateway_url_server_errors.arn,
              aws_cloudwatch_metric_alarm.registration_event_source_failures.arn,
              aws_cloudwatch_metric_alarm.registration_retry_storm.arn,
              aws_cloudwatch_metric_alarm.scheduler_delivery_failures.arn,
            ],
            [for alarm in values(aws_cloudwatch_metric_alarm.lambda_failure) : alarm.arn],
          )
        }
      },
      {
        type   = "metric"
        x      = 0
        y      = 8
        width  = 12
        height = 7
        properties = {
          title   = "Lambda compute burn rate (GB-s/s)"
          view    = "timeSeries"
          region  = var.aws_region
          period  = local.dashboard_period_seconds
          stat    = "Sum"
          stacked = false
          metrics = concat(
            local.lambda_compute_rate_metrics,
            [[{
              expression = "SUM([${join(",", [for key in keys(local.lambda_definitions) : "gbs_${key}"])}])"
              id         = "gbs_total"
              label      = "TOTAL"
            }]],
          )
          yAxis = {
            left = {
              label = "GB-s / s"
              min   = 0
            }
          }
        }
      },
      {
        type   = "metric"
        x      = 12
        y      = 8
        width  = 12
        height = 7
        properties = {
          title   = "Lambda invocation error rate (%)"
          view    = "timeSeries"
          region  = var.aws_region
          period  = local.dashboard_period_seconds
          stacked = false
          metrics = local.lambda_error_rate_metrics
          yAxis = {
            left = {
              label = "Percent"
              min   = 0
            }
          }
        }
      },
      {
        type   = "metric"
        x      = 0
        y      = 15
        width  = 12
        height = 7
        properties = {
          title   = "All Lambda failure signals"
          view    = "timeSeries"
          region  = var.aws_region
          period  = local.dashboard_period_seconds
          stat    = "Sum"
          stacked = false
          metrics = local.lambda_failure_count_metrics
          yAxis = {
            left = {
              label = "Failures / 5 min"
              min   = 0
            }
          }
        }
      },
      {
        type   = "metric"
        x      = 12
        y      = 15
        width  = 12
        height = 7
        properties = {
          title   = "Gateway Function URL responses"
          view    = "timeSeries"
          region  = var.aws_region
          period  = local.dashboard_period_seconds
          stat    = "Sum"
          stacked = false
          metrics = [
            [
              "AWS/Lambda",
              "UrlRequestCount",
              "FunctionName",
              local.function_names.gateway,
              { id = "url_requests", label = "Requests", visible = false },
            ],
            [
              "AWS/Lambda",
              "Url4xxCount",
              "FunctionName",
              local.function_names.gateway,
              { id = "url_4xx", label = "4xx count" },
            ],
            [
              "AWS/Lambda",
              "Url5xxCount",
              "FunctionName",
              local.function_names.gateway,
              { id = "url_5xx", label = "5xx count" },
            ],
            [{
              expression = "IF(url_requests > 0, 100 * FILL(url_4xx, 0) / url_requests, 0)"
              id         = "url_4xx_rate"
              label      = "4xx rate (%)"
              yAxis      = "right"
            }],
            [{
              expression = "IF(url_requests > 0, 100 * FILL(url_5xx, 0) / url_requests, 0)"
              id         = "url_5xx_rate"
              label      = "5xx rate (%)"
              yAxis      = "right"
            }],
          ]
          yAxis = {
            left = {
              label = "Responses / 5 min"
              min   = 0
            }
            right = {
              label = "Percent"
              min   = 0
            }
          }
        }
      },
      {
        type   = "metric"
        x      = 0
        y      = 22
        width  = 12
        height = 7
        properties = {
          title   = "Asynchronous Lambda event age"
          view    = "timeSeries"
          region  = var.aws_region
          period  = local.dashboard_period_seconds
          stat    = "Maximum"
          stacked = false
          metrics = local.lambda_async_age_metrics
          yAxis = {
            left = {
              label = "Milliseconds"
              min   = 0
            }
          }
        }
      },
      {
        type   = "log"
        x      = 12
        y      = 22
        width  = 12
        height = 7
        properties = {
          title  = "Recent application and runtime errors"
          region = var.aws_region
          view   = "table"
          query = join("\n| ", [
            "SOURCE logGroups(namePrefix: ['/aws/lambda/${local.name_prefix}-'])",
            "fields @timestamp, @log, level, event, @message",
            "filter level in ['ERROR', 'CRITICAL'] or @message like /(?i)(error|exception|panic|task timed out|process exited)/",
            "sort @timestamp desc",
            "limit 100",
          ])
        }
      },
      {
        type   = "metric"
        x      = 0
        y      = 29
        width  = 12
        height = 7
        properties = {
          title   = "SQS flow and retry amplification"
          view    = "timeSeries"
          region  = var.aws_region
          period  = local.dashboard_period_seconds
          stat    = "Sum"
          stacked = false
          metrics = [
            [
              "AWS/SQS",
              "NumberOfMessagesSent",
              "QueueName",
              aws_sqs_queue.registration_commands.name,
              { id = "sqs_sent", label = "Sent" },
            ],
            [
              "AWS/SQS",
              "NumberOfMessagesReceived",
              "QueueName",
              aws_sqs_queue.registration_commands.name,
              { id = "sqs_received", label = "Received" },
            ],
            [
              "AWS/SQS",
              "NumberOfMessagesDeleted",
              "QueueName",
              aws_sqs_queue.registration_commands.name,
              { id = "sqs_deleted", label = "Deleted" },
            ],
            [
              "AWS/SQS",
              "NumberOfDeduplicatedSentMessages",
              "QueueName",
              aws_sqs_queue.registration_commands.name,
              { id = "sqs_deduplicated", label = "Deduplicated sends" },
            ],
            [{
              expression = "IF(sqs_received > sqs_deleted, sqs_received - sqs_deleted, 0)"
              id         = "sqs_retry_surplus"
              label      = "Receive/delete surplus"
            }],
            [{
              expression = "IF(sqs_received > 0, sqs_received / IF(sqs_deleted > 0, sqs_deleted, 1), 0)"
              id         = "sqs_retry_amplification"
              label      = "Retry amplification"
              yAxis      = "right"
            }],
          ]
          yAxis = {
            left = {
              label = "Messages / 5 min"
              min   = 0
            }
            right = {
              label = "Receives / delete"
              min   = 0
            }
          }
        }
      },
      {
        type   = "metric"
        x      = 12
        y      = 29
        width  = 12
        height = 7
        properties = {
          title   = "SQS backlog, in-flight work, and DLQ"
          view    = "timeSeries"
          region  = var.aws_region
          period  = local.dashboard_period_seconds
          stacked = false
          metrics = [
            [
              "AWS/SQS",
              "ApproximateNumberOfMessagesVisible",
              "QueueName",
              aws_sqs_queue.registration_commands.name,
              { label = "Main queue visible", stat = "Maximum" },
            ],
            [
              "AWS/SQS",
              "ApproximateNumberOfMessagesNotVisible",
              "QueueName",
              aws_sqs_queue.registration_commands.name,
              { label = "Main queue in flight", stat = "Maximum" },
            ],
            [
              "AWS/SQS",
              "ApproximateNumberOfGroupsWithInflightMessages",
              "QueueName",
              aws_sqs_queue.registration_commands.name,
              { label = "FIFO groups in flight", stat = "Maximum" },
            ],
            [
              "AWS/SQS",
              "ApproximateAgeOfOldestMessage",
              "QueueName",
              aws_sqs_queue.registration_commands.name,
              { label = "Oldest message (seconds)", stat = "Maximum", yAxis = "right" },
            ],
            [
              "AWS/SQS",
              "ApproximateNumberOfMessagesVisible",
              "QueueName",
              aws_sqs_queue.registration_command_failures.name,
              { label = "DLQ visible", stat = "Maximum" },
            ],
          ]
          yAxis = {
            left = {
              label = "Messages / groups"
              min   = 0
            }
            right = {
              label = "Seconds"
              min   = 0
            }
          }
        }
      },
      {
        type   = "metric"
        x      = 0
        y      = 36
        width  = 12
        height = 7
        properties = {
          title   = "SQS event-source outcomes"
          view    = "timeSeries"
          region  = var.aws_region
          period  = local.dashboard_period_seconds
          stat    = "Sum"
          stacked = false
          metrics = [
            [
              "AWS/Lambda",
              "PolledEventCount",
              "EventSourceMappingUUID",
              aws_lambda_event_source_mapping.registration_commands.uuid,
              { id = "esm_polled", label = "Polled" },
            ],
            [
              "AWS/Lambda",
              "InvokedEventCount",
              "EventSourceMappingUUID",
              aws_lambda_event_source_mapping.registration_commands.uuid,
              { id = "esm_invoked", label = "Invoked" },
            ],
            [
              "AWS/Lambda",
              "DeletedEventCount",
              "EventSourceMappingUUID",
              aws_lambda_event_source_mapping.registration_commands.uuid,
              { id = "esm_deleted", label = "Deleted" },
            ],
            [
              "AWS/Lambda",
              "FailedInvokeEventCount",
              "EventSourceMappingUUID",
              aws_lambda_event_source_mapping.registration_commands.uuid,
              { id = "esm_failed", label = "Failed invoke" },
            ],
            [{
              expression = "IF(esm_invoked > esm_deleted, esm_invoked - esm_deleted, 0)"
              id         = "esm_retry_surplus"
              label      = "Invoke/delete surplus"
            }],
            [{
              expression = "IF(esm_invoked > 0, esm_invoked / IF(esm_deleted > 0, esm_deleted, 1), 0)"
              id         = "esm_retry_amplification"
              label      = "Retry amplification"
              yAxis      = "right"
            }],
          ]
          yAxis = {
            left = {
              label = "Events / 5 min"
              min   = 0
            }
            right = {
              label = "Invokes / delete"
              min   = 0
            }
          }
        }
      },
      {
        type   = "metric"
        x      = 12
        y      = 36
        width  = 12
        height = 7
        properties = {
          title   = "EventBridge Scheduler delivery"
          view    = "timeSeries"
          region  = var.aws_region
          period  = local.dashboard_period_seconds
          stat    = "Sum"
          stacked = false
          metrics = [
            for key, metric_name in {
              attempts             = "InvocationAttemptCount"
              target_errors        = "TargetErrorCount"
              target_throttles     = "TargetErrorThrottledCount"
              invocation_throttles = "InvocationThrottleCount"
              dropped              = "InvocationDroppedCount"
              } : [
              "AWS/Scheduler",
              metric_name,
              "ScheduleGroup",
              "default",
              { label = replace(key, "_", " ") },
            ]
          ]
          yAxis = {
            left = {
              label = "Attempts / failures"
              min   = 0
            }
          }
        }
      },
      {
        type   = "metric"
        x      = 0
        y      = 43
        width  = 12
        height = 7
        properties = {
          title   = "DynamoDB service and client errors"
          view    = "timeSeries"
          region  = var.aws_region
          period  = local.dashboard_period_seconds
          stat    = "Sum"
          stacked = false
          metrics = [
            [{
              expression = "SELECT SUM(SystemErrors) FROM SCHEMA(\"AWS/DynamoDB\", TableName, Operation) WHERE ${local.dynamodb_table_filter} GROUP BY TableName"
              id         = "dynamodb_system_errors"
              label      = "System errors"
            }],
            [
              "AWS/DynamoDB",
              "UserErrors",
              { label = "Account/region user errors" },
            ],
          ]
          yAxis = {
            left = {
              label = "Errors / 5 min"
              min   = 0
            }
          }
        }
      },
      {
        type   = "metric"
        x      = 12
        y      = 43
        width  = 12
        height = 7
        properties = {
          title   = "DynamoDB throttling and transaction retries"
          view    = "timeSeries"
          region  = var.aws_region
          period  = local.dashboard_period_seconds
          stat    = "Sum"
          stacked = false
          metrics = local.dynamodb_retry_metrics
          yAxis = {
            left = {
              label = "Events / 5 min"
              min   = 0
            }
          }
        }
      },
    ]
  })
}
