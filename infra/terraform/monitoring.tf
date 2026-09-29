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
      title       = "YDB SQL executions"
      description = "Cumulative ExecuteDataQuery calls over the selected interval, separate from their RU cost."
      queries {
        downsampling {
          disabled = true
        }
        target {
          query     = "alias(integral(${local.ydb_sql_requests}), \"SQL executions\")"
          text_mode = true
        }
      }
    }
  }
}
