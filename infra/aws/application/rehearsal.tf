resource "aws_dynamodb_table" "rehearsal_tg_users" {
  count = var.rehearsal_tables_enabled ? 1 : 0

  name           = local.rehearsal_table_names.tg_users
  billing_mode   = "PROVISIONED"
  read_capacity  = 1
  write_capacity = 1
  hash_key       = "tg_id"
  table_class    = "STANDARD"

  attribute {
    name = "tg_id"
    type = "S"
  }

  attribute {
    name = "telegram_handle"
    type = "S"
  }

  attribute {
    name = "linked_vk_id"
    type = "S"
  }

  global_secondary_index {
    name            = "telegram-identity"
    projection_type = "ALL"
    read_capacity   = 1
    write_capacity  = 1
    hash_key        = "telegram_handle"
    range_key       = "linked_vk_id"
  }

  global_secondary_index {
    name            = "telegram-handle"
    projection_type = "ALL"
    read_capacity   = 1
    write_capacity  = 1
    hash_key        = "telegram_handle"
  }

  deletion_protection_enabled = false

  point_in_time_recovery {
    enabled = false
  }

  server_side_encryption {
    enabled = true
  }

  tags = { Purpose = "temporary-migration-rehearsal" }
}

resource "aws_dynamodb_table" "rehearsal_vk_users" {
  count = var.rehearsal_tables_enabled ? 1 : 0

  name           = local.rehearsal_table_names.vk_users
  billing_mode   = "PROVISIONED"
  read_capacity  = 1
  write_capacity = 1
  hash_key       = "vk_id"
  table_class    = "STANDARD"

  attribute {
    name = "vk_id"
    type = "S"
  }

  deletion_protection_enabled = false

  point_in_time_recovery {
    enabled = false
  }

  server_side_encryption {
    enabled = true
  }

  tags = { Purpose = "temporary-migration-rehearsal" }
}

resource "aws_dynamodb_table" "rehearsal_events" {
  count = var.rehearsal_tables_enabled ? 1 : 0

  name           = local.rehearsal_table_names.events
  billing_mode   = "PROVISIONED"
  read_capacity  = 1
  write_capacity = 1
  hash_key       = "event_id"
  table_class    = "STANDARD"

  attribute {
    name = "event_id"
    type = "S"
  }

  attribute {
    name = "signup_token"
    type = "S"
  }

  attribute {
    name = "telegram_chat_id"
    type = "S"
  }

  global_secondary_index {
    name            = "signup-token"
    projection_type = "ALL"
    read_capacity   = 1
    write_capacity  = 1
    hash_key        = "signup_token"
  }

  global_secondary_index {
    name            = "telegram-chat"
    projection_type = "ALL"
    read_capacity   = 1
    write_capacity  = 1
    hash_key        = "telegram_chat_id"
  }

  deletion_protection_enabled = false

  point_in_time_recovery {
    enabled = false
  }

  server_side_encryption {
    enabled = true
  }

  tags = { Purpose = "temporary-migration-rehearsal" }
}

resource "aws_dynamodb_table" "rehearsal_event_leaders" {
  count = var.rehearsal_tables_enabled ? 1 : 0

  name           = local.rehearsal_table_names.event_leaders
  billing_mode   = "PROVISIONED"
  read_capacity  = 1
  write_capacity = 1
  hash_key       = "event_id"
  range_key      = "leader_key"
  table_class    = "STANDARD"

  attribute {
    name = "event_id"
    type = "S"
  }

  attribute {
    name = "leader_key"
    type = "S"
  }

  deletion_protection_enabled = false

  point_in_time_recovery {
    enabled = false
  }

  server_side_encryption {
    enabled = true
  }

  tags = { Purpose = "temporary-migration-rehearsal" }
}

resource "aws_dynamodb_table" "rehearsal_registrations" {
  count = var.rehearsal_tables_enabled ? 1 : 0

  name           = local.rehearsal_table_names.registrations
  billing_mode   = "PROVISIONED"
  read_capacity  = 1
  write_capacity = 1
  hash_key       = "event_id"
  range_key      = "participant_key"
  table_class    = "STANDARD"

  attribute {
    name = "event_id"
    type = "S"
  }

  attribute {
    name = "participant_key"
    type = "S"
  }

  attribute {
    name = "telegram_profile"
    type = "S"
  }

  global_secondary_index {
    name            = "telegram-profile"
    projection_type = "ALL"
    read_capacity   = 1
    write_capacity  = 1
    hash_key        = "telegram_profile"
    range_key       = "event_id"
  }

  deletion_protection_enabled = false

  point_in_time_recovery {
    enabled = false
  }

  server_side_encryption {
    enabled = true
  }

  tags = { Purpose = "temporary-migration-rehearsal" }
}
