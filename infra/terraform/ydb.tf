resource "yandex_ydb_database_serverless" "application" {
  name                = var.project_name
  folder_id           = var.yandex_folder_id
  deletion_protection = true
  labels              = local.labels

  serverless_database {
    enable_throttling_rcu_limit = true
    throttling_rcu_limit        = 20
    provisioned_rcu_limit       = 0
    storage_size_limit          = 5
  }
}

locals {
  common_user_columns = [
    { name = "full_name", type = "Utf8", not_null = false },
    { name = "crossplay", type = "Bool", not_null = false },
    { name = "larp_experience", type = "Bool", not_null = false },
    { name = "needs_pass", type = "Bool", not_null = false },
    { name = "pass_details_json", type = "Utf8", not_null = false },
    { name = "dialog_state", type = "Utf8", not_null = true },
    { name = "dialog_context_json", type = "Utf8", not_null = true },
    { name = "last_update_id", type = "Utf8", not_null = false },
    { name = "last_update_at", type = "Timestamp", not_null = false },
    { name = "last_delivery_operation_id", type = "Utf8", not_null = false },
    { name = "created_at", type = "Timestamp", not_null = true },
    { name = "updated_at", type = "Timestamp", not_null = true },
  ]
}

# HARD STORAGE INVARIANT: these are the only three application YDB tables.
resource "yandex_ydb_table" "tg_users" {
  path              = "tg_users"
  connection_string = yandex_ydb_database_serverless.application.ydb_full_endpoint

  column {
    name     = "tg_id"
    type     = "Uint64"
    not_null = true
  }
  column {
    name = "vk_url"
    type = "Utf8"
  }
  dynamic "column" {
    for_each = local.common_user_columns
    content {
      name     = column.value.name
      type     = column.value.type
      not_null = column.value.not_null
    }
  }
  primary_key = ["tg_id"]
}

resource "yandex_ydb_table" "vk_users" {
  path              = "vk_users"
  connection_string = yandex_ydb_database_serverless.application.ydb_full_endpoint

  column {
    name     = "vk_id"
    type     = "Uint64"
    not_null = true
  }
  column {
    name = "telegram_handle"
    type = "Utf8"
  }
  dynamic "column" {
    for_each = local.common_user_columns
    content {
      name     = column.value.name
      type     = column.value.type
      not_null = column.value.not_null
    }
  }
  primary_key = ["vk_id"]
}

resource "yandex_ydb_table" "events" {
  path              = "events"
  connection_string = yandex_ydb_database_serverless.application.ydb_full_endpoint

  column {
    name     = "event_id"
    type     = "Utf8"
    not_null = true
  }
  column {
    name     = "name"
    type     = "Utf8"
    not_null = true
  }
  column {
    name     = "disk_resource_path"
    type     = "Utf8"
    not_null = true
  }
  column {
    name     = "public_registration_url"
    type     = "Utf8"
    not_null = true
  }
  column {
    name     = "status"
    type     = "Utf8"
    not_null = true
  }
  column {
    name     = "created_at"
    type     = "Timestamp"
    not_null = true
  }
  column {
    name     = "updated_at"
    type     = "Timestamp"
    not_null = true
  }
  primary_key = ["event_id"]
}
