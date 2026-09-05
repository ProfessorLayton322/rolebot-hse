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

# The control-plane operation may complete before a new serverless database is
# discoverable through its data-plane endpoint. Without this one-time wait the
# YDB table provider treats the transient DatabaseNotFound response as fatal.
resource "time_sleep" "ydb_ready" {
  depends_on      = [yandex_ydb_database_serverless.application]
  create_duration = "90s"

  triggers = {
    database_id = yandex_ydb_database_serverless.application.id
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
    { name = "last_bot_buttons_json", type = "Utf8", not_null = false },
    { name = "is_gamemaster", type = "Bool", not_null = false },
    { name = "gamemaster_grant_operation_id", type = "Utf8", not_null = false },
    { name = "created_at", type = "Timestamp", not_null = true },
    { name = "updated_at", type = "Timestamp", not_null = true },
  ]
}

# Application tables. Registrations are clustered by event for inexpensive
# point lookups and full-game showcase projections.
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
  primary_key = ["tg_id"]

  depends_on = [time_sleep.ydb_ready]
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

  depends_on = [time_sleep.ydb_ready]
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
    name = "public_table_resource_path"
    type = "Utf8"
  }
  column {
    name = "public_table_public_url"
    type = "Utf8"
  }
  column {
    name     = "status"
    type     = "Utf8"
    not_null = true
  }
  column {
    name = "confirmation_deadline"
    type = "Timestamp"
  }
  column {
    name = "registrations_migrated_at"
    type = "Timestamp"
  }
  column {
    name = "pass_table_resource_path"
    type = "Utf8"
  }
  column {
    name = "pass_table_public_url"
    type = "Utf8"
  }
  column {
    name = "confirmed_notifications_json"
    type = "Utf8"
  }
  column {
    name = "last_confirmed_notification_operation_id"
    type = "Utf8"
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

  depends_on = [time_sleep.ydb_ready]
}

# Privileged users are attached to individual games through this normalized
# membership list. A composite primary key makes repeated grants idempotent.
resource "yandex_ydb_table" "event_leaders" {
  path              = "event_leaders"
  connection_string = yandex_ydb_database_serverless.application.ydb_full_endpoint

  column {
    name     = "event_id"
    type     = "Utf8"
    not_null = true
  }
  column {
    name     = "platform"
    type     = "Utf8"
    not_null = true
  }
  column {
    name     = "platform_user_id"
    type     = "Uint64"
    not_null = true
  }
  column {
    name     = "created_at"
    type     = "Timestamp"
    not_null = true
  }
  primary_key = ["event_id", "platform", "platform_user_id"]

  depends_on = [time_sleep.ydb_ready]
}

resource "yandex_ydb_table" "registrations" {
  path              = "registrations"
  connection_string = yandex_ydb_database_serverless.application.ydb_full_endpoint

  column {
    name     = "event_id"
    type     = "Utf8"
    not_null = true
  }
  column {
    name     = "participant_key"
    type     = "Utf8"
    not_null = true
  }
  column {
    name     = "display_name"
    type     = "Utf8"
    not_null = true
  }
  column {
    name = "vk_profile"
    type = "Utf8"
  }
  column {
    name = "telegram_profile"
    type = "Utf8"
  }
  column {
    name     = "wish_play"
    type     = "Utf8"
    not_null = true
  }
  column {
    name = "larp_experience"
    type = "Bool"
  }
  column {
    name = "crossplay"
    type = "Bool"
  }
  column {
    name     = "character_wish"
    type     = "Utf8"
    not_null = true
  }
  column {
    name     = "attendance_status"
    type     = "Utf8"
    not_null = true
  }
  column {
    name     = "last_operation_id"
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
  primary_key = ["event_id", "participant_key"]

  depends_on = [time_sleep.ydb_ready]
}
