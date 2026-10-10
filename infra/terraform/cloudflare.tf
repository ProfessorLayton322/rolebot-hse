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

  # Routing values are non-secret so Terraform can switch them atomically with
  # the Worker code. The rate-limit namespace is account-wide: another Worker
  # binding the same ID would share these counters. `wrangler secret bulk`
  # patches only secrets, so every binding below carries over to that upload.
  bindings = [
    {
      type = "plain_text"
      name = "ACTIVE_BACKEND"
      text = var.cloudflare_active_backend
    },
    {
      type = "plain_text"
      name = "YANDEX_BACKEND_URL"
      text = "https://${yandex_api_gateway.bot.domain}"
    },
    {
      type = "plain_text"
      name = "AWS_BACKEND_URL"
      text = var.aws_backend_url
    },
    {
      type         = "ratelimit"
      name         = "TG_USER_RATE_LIMITER"
      namespace_id = "731001"
      simple = {
        limit  = var.telegram_user_rate_limit_per_minute
        period = 60
      }
    }
  ]
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
