resource "aws_dynamodb_table" "tg_users" {
  name           = local.table_names.tg_users
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

    key_schema {
      attribute_name = "telegram_handle"
      key_type       = "HASH"
    }

    key_schema {
      attribute_name = "linked_vk_id"
      key_type       = "RANGE"
    }
  }

  global_secondary_index {
    name            = "telegram-handle"
    projection_type = "ALL"
    read_capacity   = 1
    write_capacity  = 1

    key_schema {
      attribute_name = "telegram_handle"
      key_type       = "HASH"
    }
  }

  deletion_protection_enabled = true

  point_in_time_recovery {
    enabled = false
  }

  server_side_encryption {
    enabled = true
  }

  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_dynamodb_table" "vk_users" {
  name           = local.table_names.vk_users
  billing_mode   = "PROVISIONED"
  read_capacity  = 1
  write_capacity = 1
  hash_key       = "vk_id"
  table_class    = "STANDARD"

  attribute {
    name = "vk_id"
    type = "S"
  }

  deletion_protection_enabled = true

  point_in_time_recovery {
    enabled = false
  }

  server_side_encryption {
    enabled = true
  }

  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_dynamodb_table" "events" {
  name           = local.table_names.events
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

    key_schema {
      attribute_name = "signup_token"
      key_type       = "HASH"
    }
  }

  global_secondary_index {
    name            = "telegram-chat"
    projection_type = "ALL"
    read_capacity   = 1
    write_capacity  = 1

    key_schema {
      attribute_name = "telegram_chat_id"
      key_type       = "HASH"
    }
  }

  deletion_protection_enabled = true

  point_in_time_recovery {
    enabled = false
  }

  server_side_encryption {
    enabled = true
  }

  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_dynamodb_table" "event_leaders" {
  name           = local.table_names.event_leaders
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

  # The reversible adapter encodes platform and platform_user_id into this
  # lossless sort key while retaining both original fields in the item.
  attribute {
    name = "leader_key"
    type = "S"
  }

  deletion_protection_enabled = true

  point_in_time_recovery {
    enabled = false
  }

  server_side_encryption {
    enabled = true
  }

  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_dynamodb_table" "registrations" {
  name           = local.table_names.registrations
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

    key_schema {
      attribute_name = "telegram_profile"
      key_type       = "HASH"
    }

    key_schema {
      attribute_name = "event_id"
      key_type       = "RANGE"
    }
  }

  deletion_protection_enabled = true

  point_in_time_recovery {
    enabled = false
  }

  server_side_encryption {
    enabled = true
  }

  lifecycle {
    prevent_destroy = true
  }
}
