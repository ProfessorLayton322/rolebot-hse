output "yandex_gateway_url" {
  value       = "https://${yandex_api_gateway.bot.domain}/webhooks/telegram"
  description = "Internal Telegram target used only by Cloudflare ingress."
}

output "yandex_backend_url" {
  value       = "https://${yandex_api_gateway.bot.domain}"
  description = "Yandex backend base URL used by the reversible Cloudflare router."
}

output "vk_callback_url" {
  value       = "${local.telegram_ingress_url}/vk"
  description = "Stable Cloudflare URL to configure in VK Callback API."
}

output "yandex_vk_rollback_url" {
  value       = "https://${yandex_api_gateway.bot.domain}/webhooks/vk"
  description = "Direct Yandex VK callback retained only as a rollback target."
}

output "telegram_ingress_url" {
  value       = local.telegram_ingress_url
  description = "Public Telegram webhook base URL."
}

output "telegram_egress_url" {
  value       = local.telegram_egress_url
  description = "Private signed Telegram egress base URL."
}

output "runtime_service_account_ids" {
  value = jsonencode([
    yandex_iam_service_account.gateway.id,
    yandex_iam_service_account.worker.id,
    yandex_iam_service_account.disk_uploader.id,
  ])
  description = "Allowed OIDC subjects for the Cloudflare runtime config endpoint."
}

output "ymq_access_key_id" {
  value     = yandex_iam_service_account_static_access_key.ymq_client.access_key
  sensitive = true
}

output "ymq_secret_access_key" {
  value     = yandex_iam_service_account_static_access_key.ymq_client.secret_key
  sensitive = true
}

output "ymq_kick_url" {
  value       = yandex_message_queue.worker_kicks.id
  description = "Standard queue used to wake the ordered FIFO worker."
}

output "ydb_database_path" {
  value = yandex_ydb_database_serverless.application.database_path
}

output "application_ydb_tables" {
  value       = [yandex_ydb_table.tg_users.path, yandex_ydb_table.vk_users.path, yandex_ydb_table.events.path]
  description = "CI contract: must contain exactly tg_users, vk_users, events."
}
