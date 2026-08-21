locals {
  labels = merge(var.labels, {
    application = var.project_name
    managed_by  = "terraform"
  })

  telegram_ingress_name = "${var.project_name}-telegram-ingress"
  telegram_egress_name  = "${var.project_name}-telegram-egress"
  telegram_ingress_url  = "https://${local.telegram_ingress_name}.${var.cloudflare_workers_subdomain}.workers.dev"
  telegram_egress_url   = "https://${local.telegram_egress_name}.${var.cloudflare_workers_subdomain}.workers.dev"
}
