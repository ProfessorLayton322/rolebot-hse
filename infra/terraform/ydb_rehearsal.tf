resource "yandex_ydb_database_serverless" "rehearsal" {
  count = var.ydb_rehearsal_enabled ? 1 : 0

  name                = "${var.project_name}-rehearsal"
  folder_id           = var.yandex_folder_id
  deletion_protection = false
  labels              = merge(local.labels, { purpose = "temporary-migration-rehearsal" })

  serverless_database {
    enable_throttling_rcu_limit = true
    throttling_rcu_limit        = 10
    provisioned_rcu_limit       = 0
    storage_size_limit          = 1
  }
}

resource "time_sleep" "rehearsal_ydb_ready" {
  count = var.ydb_rehearsal_enabled ? 1 : 0

  depends_on      = [yandex_ydb_database_serverless.rehearsal]
  create_duration = "90s"

  triggers = {
    database_id = yandex_ydb_database_serverless.rehearsal[0].id
  }
}

locals {
  rehearsal_ydb_schemas = {
    tg_users = {
      primary_key = ["tg_id"]
      columns = concat([
        { name = "tg_id", type = "Uint64", not_null = true },
        { name = "vk_url", type = "Utf8", not_null = false },
        { name = "telegram_handle", type = "Utf8", not_null = false },
        { name = "linked_vk_id", type = "Uint64", not_null = false },
      ], local.common_user_columns)
    }
    vk_users = {
      primary_key = ["vk_id"]
      columns = concat([
        { name = "vk_id", type = "Uint64", not_null = true },
        { name = "telegram_handle", type = "Utf8", not_null = false },
      ], local.common_user_columns)
    }
    events = {
      primary_key = ["event_id"]
      columns = [
        { name = "event_id", type = "Utf8", not_null = true },
        { name = "name", type = "Utf8", not_null = true },
        { name = "game_date", type = "Date", not_null = false },
        { name = "player_amount", type = "Uint64", not_null = false },
        { name = "signup_token", type = "Utf8", not_null = false },
        { name = "telegram_post_url", type = "Utf8", not_null = false },
        { name = "vk_post_url", type = "Utf8", not_null = false },
        { name = "telegram_chat_id", type = "Int64", not_null = false },
        { name = "telegram_chat_invite_link", type = "Utf8", not_null = false },
        { name = "disk_resource_path", type = "Utf8", not_null = true },
        { name = "public_registration_url", type = "Utf8", not_null = true },
        { name = "master_table_google_url", type = "Utf8", not_null = false },
        { name = "public_table_resource_path", type = "Utf8", not_null = false },
        { name = "public_table_public_url", type = "Utf8", not_null = false },
        { name = "public_table_google_url", type = "Utf8", not_null = false },
        { name = "status", type = "Utf8", not_null = true },
        { name = "registration_open_at", type = "Timestamp", not_null = false },
        { name = "registration_open_notified_recipients_json", type = "Utf8", not_null = false },
        { name = "registration_open_notification_completed_at", type = "Timestamp", not_null = false },
        { name = "scheduled_close_at", type = "Timestamp", not_null = false },
        { name = "scheduled_close_operation_id", type = "Utf8", not_null = false },
        { name = "scheduled_close_notified_recipients_json", type = "Utf8", not_null = false },
        { name = "scheduled_close_completed_at", type = "Timestamp", not_null = false },
        { name = "confirmation_open_at", type = "Timestamp", not_null = false },
        { name = "confirmation_deadline", type = "Timestamp", not_null = false },
        { name = "registrations_migrated_at", type = "Timestamp", not_null = false },
        { name = "pass_table_resource_path", type = "Utf8", not_null = false },
        { name = "pass_table_public_url", type = "Utf8", not_null = false },
        { name = "pass_table_google_url", type = "Utf8", not_null = false },
        { name = "achievement_resource_path", type = "Utf8", not_null = false },
        { name = "achievement_public_url", type = "Utf8", not_null = false },
        { name = "confirmed_notifications_json", type = "Utf8", not_null = false },
        { name = "last_confirmed_notification_operation_id", type = "Utf8", not_null = false },
        { name = "last_reserve_promotion_operation_id", type = "Utf8", not_null = false },
        { name = "last_reserve_promotion_participant_key", type = "Utf8", not_null = false },
        { name = "last_reserve_promotion_delivered_operation_id", type = "Utf8", not_null = false },
        { name = "last_confirmed_link_promotion_operation_id", type = "Utf8", not_null = false },
        { name = "last_confirmed_link_promotion_participant_key", type = "Utf8", not_null = false },
        { name = "last_confirmed_link_promotion_delivered_operation_id", type = "Utf8", not_null = false },
        { name = "last_participant_removal_operation_id", type = "Utf8", not_null = false },
        { name = "participant_removal_keys_json", type = "Utf8", not_null = false },
        { name = "participant_removal_notified_keys_json", type = "Utf8", not_null = false },
        { name = "archived_at", type = "Timestamp", not_null = false },
        { name = "created_at", type = "Timestamp", not_null = true },
        { name = "updated_at", type = "Timestamp", not_null = true },
      ]
    }
    event_leaders = {
      primary_key = ["event_id", "platform", "platform_user_id"]
      columns = [
        { name = "event_id", type = "Utf8", not_null = true },
        { name = "platform", type = "Utf8", not_null = true },
        { name = "platform_user_id", type = "Uint64", not_null = true },
        { name = "created_at", type = "Timestamp", not_null = true },
      ]
    }
    registrations = {
      primary_key = ["event_id", "participant_key"]
      columns = [
        { name = "event_id", type = "Utf8", not_null = true },
        { name = "participant_key", type = "Utf8", not_null = true },
        { name = "display_name", type = "Utf8", not_null = true },
        { name = "nickname", type = "Utf8", not_null = false },
        { name = "vk_profile", type = "Utf8", not_null = false },
        { name = "telegram_profile", type = "Utf8", not_null = false },
        { name = "wish_play", type = "Utf8", not_null = true },
        { name = "larp_experience", type = "Bool", not_null = false },
        { name = "crossplay", type = "Bool", not_null = false },
        { name = "character_wish", type = "Utf8", not_null = true },
        { name = "attendance_status", type = "Utf8", not_null = true },
        { name = "last_operation_id", type = "Utf8", not_null = true },
        { name = "created_at", type = "Timestamp", not_null = true },
        { name = "updated_at", type = "Timestamp", not_null = true },
        { name = "participation_notification_deliveries_json", type = "Utf8", not_null = false },
      ]
    }
  }
}

resource "yandex_ydb_table" "rehearsal" {
  for_each = var.ydb_rehearsal_enabled ? local.rehearsal_ydb_schemas : {}

  path              = each.key
  connection_string = yandex_ydb_database_serverless.rehearsal[0].ydb_full_endpoint

  dynamic "column" {
    for_each = each.value.columns
    content {
      name     = column.value.name
      type     = column.value.type
      not_null = column.value.not_null
    }
  }

  primary_key = each.value.primary_key
  depends_on  = [time_sleep.rehearsal_ydb_ready]
}

resource "yandex_ydb_table_index" "rehearsal_telegram_identity" {
  count = var.ydb_rehearsal_enabled ? 1 : 0

  table_id = yandex_ydb_table.rehearsal["tg_users"].id
  name     = "telegram_identity"
  type     = "global_sync"
  columns  = ["telegram_handle", "linked_vk_id"]
}
