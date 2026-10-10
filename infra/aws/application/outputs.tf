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

output "runtime_parameter_names" {
  description = "Stable SSM names populated by the manual deployment workflow."
  value       = local.parameter_names
}

output "standby_controls" {
  description = "Controls that remain disabled until the production cutover sequence."
  value = {
    application_mode        = "standby"
    mutation_enabled        = false
    gateway_auth_type       = aws_lambda_function_url.gateway.authorization_type
    sqs_event_mapping_state = aws_lambda_event_source_mapping.registration_commands.enabled
    scheduler_state         = aws_scheduler_schedule.registration_notifications.state
  }
}
