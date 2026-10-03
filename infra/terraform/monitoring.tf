# Adopt the existing dashboard instead of creating a second one. Imports and
# updates run only through the Terraform GitHub Actions workflow.
import {
  to = yandex_monitoring_dashboard.ydb_operations
  id = "fbehpu9s0f63vvj3p2v9"
}

locals {
  ydb_sql_requests = "series_sum(\"api.grpc.request.count\"{folderId=\"${var.yandex_folder_id}\", service=\"ydb\", database.serverless=\"${var.project_name}\", method=\"ExecuteDataQuery\"})"
  ydb_api_ru       = "series_sum(\"api.units.consumed_per_second\"{folderId=\"${var.yandex_folder_id}\", service=\"ydb\", database.serverless=\"${var.project_name}\"})"

  function_gb_hour_charts = {
    disk-uploader = {
      title       = "Disk uploader compute (GB-hours)"
      function_id = yandex_function.disk_uploader.id
      memory_mb   = yandex_function.disk_uploader.memory
      x           = 0
      y           = 16
    }
    gateway = {
      title       = "Gateway compute (GB-hours)"
      function_id = yandex_function.gateway.id
      memory_mb   = yandex_function.gateway.memory
      x           = 12
      y           = 16
    }
    ordered-worker = {
      title       = "Ordered worker compute (GB-hours)"
      function_id = yandex_function.ordered_worker.id
      memory_mb   = yandex_function.ordered_worker.memory
      x           = 0
      y           = 24
    }
    registration-notifier = {
      title       = "Registration notifier compute (GB-hours)"
      function_id = yandex_function.registration_notifier.id
      memory_mb   = yandex_function.registration_notifier.memory
      x           = 12
      y           = 24
    }
    upload-completion = {
      title       = "Upload completion compute (GB-hours)"
      function_id = yandex_function.upload_completion.id
      memory_mb   = yandex_function.upload_completion.memory
      x           = 0
      y           = 32
    }
  }

  # This histogram is an execution-duration rate. Its sum is milliseconds of
  # execution per second, which integral() converts to cumulative milliseconds.
  function_execution_time_rates = {
    for name, function in local.function_gb_hour_charts :
    name => "series_sum(histogram_sum(\"bin\", \"serverless.functions.execution_time_milliseconds\"{folderId=\"${var.yandex_folder_id}\", service=\"serverless-functions\", function=\"${function.function_id}\"}))"
  }
  function_inflight = {
    for name, function in local.function_gb_hour_charts :
    name => "series_sum(\"serverless.functions.inflight\"{folderId=\"${var.yandex_folder_id}\", service=\"serverless-functions\", function=\"${function.function_id}\"})"
  }
  function_gb_hour_queries = {
    for name, function in local.function_gb_hour_charts :
    name => "integral(replace_nan(${local.function_execution_time_rates[name]}, 0) + 0 * ${local.function_inflight[name]}) * ${function.memory_mb} / 1024 / 3600000"
  }
  function_total_gb_hour_query = join(" + ", [
    for query in values(local.function_gb_hour_queries) : "(${query})"
  ])
}

resource "yandex_monitoring_dashboard" "ydb_operations" {
  folder_id = var.yandex_folder_id
  name      = "ydb-operations-dash"
  title     = "ydb operations dash"

  widgets {
    position {
      x = 0
      y = 0
      w = 12
      h = 8
    }
    chart {
      chart_id    = "ydb-consumed-ru"
      title       = "Consumed YDB request units (RU)"
      description = "Cumulative RU over the selected interval. RU measure query cost, not the number of requests."
      queries {
        # The legacy API RU gauge omits idle samples. integral() otherwise
        # multiplies the next spike by the entire preceding idle gap. Align to
        # the continuous 15-second request counter before integrating. Coarse
        # AVG downsampling of the sparse gauge would reintroduce overcounting.
        downsampling {
          disabled = true
        }
        target {
          query     = "alias(integral(replace_nan(${local.ydb_api_ru}, 0) + 0 * ${local.ydb_sql_requests}), \"Consumed RU\")"
          text_mode = true
        }
      }
    }
  }

  widgets {
    position {
      x = 12
      y = 0
      w = 12
      h = 8
    }
    chart {
      chart_id    = "ydb-sql-execution-rate"
      title       = "YDB SQL execution rate"
      description = "ExecuteDataQuery calls per second. The scheduled lifecycle check normally produces one isolated call every hour."
      queries {
        downsampling {
          disabled = true
        }
        target {
          # This metric is already a request rate. Integrating it produced a
          # monotonically increasing interval total that looked like a backlog.
          query     = "alias(${local.ydb_sql_requests}, \"SQL executions/s\")"
          text_mode = true
        }
      }
    }
  }

  widgets {
    position {
      x = 0
      y = 8
      w = 24
      h = 8
    }
    chart {
      chart_id    = "functions-total-gb-hours"
      title       = "Total function compute (GB-hours)"
      description = "Cumulative execution-time consumption for all deployed functions over the selected interval. Billing can be slightly higher because each invocation is rounded to its billing duration."
      queries {
        downsampling {
          disabled = true
        }
        target {
          query     = "alias(${local.function_total_gb_hour_query}, \"Total consumed GB-hours\")"
          text_mode = true
        }
      }
    }
  }

  dynamic "widgets" {
    for_each = local.function_gb_hour_charts

    content {
      position {
        x = widgets.value.x
        y = widgets.value.y
        w = 12
        h = 8
      }
      chart {
        chart_id    = "function-${widgets.key}-gb-hours"
        title       = widgets.value.title
        description = "Cumulative execution-time consumption over the selected interval, derived from the execution-time rate and configured memory. The value resets at the left edge of the selected range."
        queries {
          # The in-flight metric supplies regular zero-valued timestamps so
          # idle gaps cannot inflate the cumulative execution-time integral.
          downsampling {
            disabled = true
          }
          target {
            query     = "alias(${local.function_gb_hour_queries[widgets.key]}, \"Consumed GB-hours\")"
            text_mode = true
          }
        }
      }
    }
  }
}
