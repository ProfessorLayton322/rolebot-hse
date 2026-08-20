resource "yandex_sws_advanced_rate_limiter_profile" "webhooks" {
  name        = "${var.project_name}-webhook-arl"
  description = "Global webhook limit; intentionally not grouped by Cloudflare source IP"
  folder_id   = var.yandex_folder_id
  labels      = local.labels

  advanced_rate_limiter_rule {
    name        = "webhook-global-rps"
    description = "Conservative shared cap for the two small-bot webhook routes"
    priority    = 1000

    dynamic_quota {
      action = "DENY"
      limit  = var.webhook_rate_limit_rps
      period = 1
    }
  }
}

resource "yandex_sws_security_profile" "api_gateway" {
  name                             = "${var.project_name}-api-gateway"
  description                      = "L7 protection for Telegram and VK callbacks"
  folder_id                        = var.yandex_folder_id
  default_action                   = "ALLOW"
  disallow_data_processing         = false
  advanced_rate_limiter_profile_id = yandex_sws_advanced_rate_limiter_profile.webhooks.id
  labels                           = local.labels

  security_rule {
    name     = "api-smart-protection"
    priority = 999900
    smart_protection {
      mode = "API"
    }
  }
}
