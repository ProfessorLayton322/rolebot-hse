# Adopt the existing dashboard instead of creating a second one. Imports and
# updates run only through the Terraform GitHub Actions workflow.
import {
  to = yandex_monitoring_dashboard.ydb_operations
  id = "fbehpu9s0f63vvj3p2v9"
}

locals {
  ydb_sql_requests = "series_sum(\"api.grpc.request.count\"{folderId=\"${var.yandex_folder_id}\", service=\"ydb\", database.serverless=\"${var.project_name}\", method=\"ExecuteDataQuery\"})"
  ydb_api_ru       = "series_sum(\"api.units.consumed_per_second\"{folderId=\"${var.yandex_folder_id}\", service=\"ydb\", database.serverless=\"${var.project_name}\"})"
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
      description = "ExecuteDataQuery calls per second. The scheduled lifecycle check normally produces one isolated call every five minutes."
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
}
