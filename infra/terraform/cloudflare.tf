resource "cloudflare_worker" "telegram_ingress" {
  account_id = var.cloudflare_account_id
  name       = local.telegram_ingress_name

  # cloudflare_worker also manages this setting. Leaving it unspecified makes
  # the provider set enabled=false even when the dedicated subdomain resource
  # below requests true, so the public webhook disappears on every apply.
  subdomain = {
    enabled          = true
    previews_enabled = false
  }
}

resource "cloudflare_worker_version" "telegram_ingress" {
  account_id         = var.cloudflare_account_id
  worker_id          = cloudflare_worker.telegram_ingress.id
  compatibility_date = "2026-08-20"
  main_module        = "index.js"

  modules = [{
    name         = "index.js"
    content_file = "${path.module}/../../cloudflare/telegram-ingress/dist/index.js"
    content_type = "application/javascript+module"
  }]
}

resource "cloudflare_workers_deployment" "telegram_ingress" {
  account_id  = var.cloudflare_account_id
  script_name = cloudflare_worker.telegram_ingress.name
  strategy    = "percentage"
  versions = [{
    version_id = cloudflare_worker_version.telegram_ingress.id
    percentage = 100
  }]
}

resource "cloudflare_workers_script_subdomain" "telegram_ingress" {
  account_id       = var.cloudflare_account_id
  script_name      = cloudflare_worker.telegram_ingress.name
  enabled          = true
  previews_enabled = false
}

resource "cloudflare_worker" "telegram_egress" {
  account_id = var.cloudflare_account_id
  name       = local.telegram_egress_name

  subdomain = {
    enabled          = true
    previews_enabled = false
  }
}

resource "cloudflare_worker_version" "telegram_egress" {
  account_id         = var.cloudflare_account_id
  worker_id          = cloudflare_worker.telegram_egress.id
  compatibility_date = "2026-08-20"
  main_module        = "index.js"

  modules = [{
    name         = "index.js"
    content_file = "${path.module}/../../cloudflare/telegram-egress/dist/index.js"
    content_type = "application/javascript+module"
  }]
}

resource "cloudflare_workers_deployment" "telegram_egress" {
  account_id  = var.cloudflare_account_id
  script_name = cloudflare_worker.telegram_egress.name
  strategy    = "percentage"
  versions = [{
    version_id = cloudflare_worker_version.telegram_egress.id
    percentage = 100
  }]
}

resource "cloudflare_workers_script_subdomain" "telegram_egress" {
  account_id       = var.cloudflare_account_id
  script_name      = cloudflare_worker.telegram_egress.name
  enabled          = true
  previews_enabled = false
}

# Secret bindings are installed after this deployment by `wrangler secret bulk`.
# That keeps TG_BOT_TOKEN and both transport secrets out of Terraform state.
