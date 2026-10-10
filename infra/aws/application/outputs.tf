output "standby_gateway_url" {
  description = "Public Lambda URL protected by application HMAC and fixed standby runtime controls."
  value       = aws_lambda_function_url.gateway.function_url
}

output "dynamodb_tables" {
  description = "Mirrored table names used by migration tooling in later phases."
  value       = local.table_names
}

output "registration_fifo_url" {
  description = "Disabled-consumer FIFO queue URL."
  value       = aws_sqs_queue.registration_commands.id
}

output "application_bucket" {
  description = "Private application staging bucket."
  value       = aws_s3_bucket.application.id
}

output "telegram_egress_url" {
  description = "Existing dual-signature Cloudflare Telegram egress endpoint."
  value       = "https://${var.project_name}-telegram-egress.${var.cloudflare_workers_subdomain}.workers.dev"
}

output "runtime_parameter_names" {
  description = "Stable SSM names populated by the manual deployment workflow."
  value       = local.parameter_names
}

output "standby_controls" {
  description = "Current reversible runtime and event-source controls."
  value = {
    application_mode        = var.application_mode
    mutation_enabled        = var.mutations_enabled
    runtime_table_target    = var.runtime_table_target
    rehearsal_tables        = var.rehearsal_tables_enabled
    gateway_auth_type       = aws_lambda_function_url.gateway.authorization_type
    sqs_event_mapping_state = aws_lambda_event_source_mapping.registration_commands.enabled
    scheduler_state         = aws_scheduler_schedule.registration_notifications.state
  }
}

output "rehearsal_table_prefix" {
  description = "Temporary DynamoDB prefix when Phase 5 tables are enabled."
  value       = var.rehearsal_tables_enabled ? "${var.project_name}-rehearsal" : null
}
