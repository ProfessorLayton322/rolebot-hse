resource "yandex_logging_group" "application" {
  name             = "${var.project_name}-functions"
  description      = "Structured gateway and ordered-worker logs"
  folder_id        = var.yandex_folder_id
  retention_period = "72h"
  labels           = local.labels
}
