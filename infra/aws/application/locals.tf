locals {
  name_prefix = "${var.project_name}-${var.environment}"

  tags = {
    Application = var.project_name
    Environment = var.environment
    ManagedBy   = "terraform"
    Cloud       = "aws"
  }

  table_names = {
    tg_users      = "${local.name_prefix}-tg-users"
    vk_users      = "${local.name_prefix}-vk-users"
    events        = "${local.name_prefix}-events"
    event_leaders = "${local.name_prefix}-event-leaders"
    registrations = "${local.name_prefix}-registrations"
  }

  function_names = {
    gateway               = "${local.name_prefix}-gateway"
    ordered_worker        = "${local.name_prefix}-ordered-worker"
    registration_notifier = "${local.name_prefix}-registration-notifier"
    disk_uploader         = "${local.name_prefix}-disk-uploader"
    upload_completion     = "${local.name_prefix}-upload-completion"
  }

  parameter_names = {
    tg_bot_token                   = "/${var.project_name}/production/tg-bot-token"
    tg_webhook_secret              = "/${var.project_name}/production/tg-webhook-secret"
    vk_access_token                = "/${var.project_name}/production/vk-access-token"
    vk_callback_secret             = "/${var.project_name}/production/vk-callback-secret"
    vk_confirmation_string         = "/${var.project_name}/production/vk-confirmation-string"
    yandex_disk_token              = "/${var.project_name}/production/yandex-disk-token"
    google_service_account_json    = "/${var.project_name}/production/google-service-account-json"
    participant_key_hmac_secret    = "/${var.project_name}/production/participant-key-hmac-secret"
    cloudflare_ingress_hmac_secret = "/${var.project_name}/production/cloudflare-ingress-hmac-secret"
    cloudflare_egress_hmac_secret  = "/${var.project_name}/production/cloudflare-egress-hmac-secret"
  }

  parameter_arns = {
    for key, name in local.parameter_names :
    key => "arn:aws:ssm:${var.aws_region}:${var.aws_account_id}:parameter${name}"
  }

  lambda_parameter_keys = {
    gateway = [
      "tg_bot_token",
      "tg_webhook_secret",
      "vk_access_token",
      "vk_callback_secret",
      "vk_confirmation_string",
      "participant_key_hmac_secret",
      "cloudflare_ingress_hmac_secret",
      "cloudflare_egress_hmac_secret",
    ]
    ordered_worker = [
      "tg_bot_token",
      "vk_access_token",
      "google_service_account_json",
      "participant_key_hmac_secret",
      "cloudflare_egress_hmac_secret",
    ]
    registration_notifier = [
      "tg_bot_token",
      "vk_access_token",
      "participant_key_hmac_secret",
      "cloudflare_egress_hmac_secret",
    ]
    disk_uploader = [
      "yandex_disk_token",
      "cloudflare_egress_hmac_secret",
    ]
    upload_completion = [
      "tg_bot_token",
      "vk_access_token",
      "cloudflare_egress_hmac_secret",
    ]
  }

  lambda_definitions = {
    gateway = {
      runtime     = "python3.12"
      handler     = "handler.handler"
      package     = var.python_standby_zip_path
      memory_size = 512
      timeout     = 30
    }
    ordered_worker = {
      runtime     = "python3.12"
      handler     = "handler.handler"
      package     = var.python_standby_zip_path
      memory_size = 1024
      timeout     = 150
    }
    registration_notifier = {
      runtime     = "provided.al2023"
      handler     = "bootstrap"
      package     = var.go_standby_zip_path
      memory_size = 128
      timeout     = 60
    }
    disk_uploader = {
      runtime     = "provided.al2023"
      handler     = "bootstrap"
      package     = var.go_standby_zip_path
      memory_size = 128
      timeout     = 300
    }
    upload_completion = {
      runtime     = "python3.12"
      handler     = "handler.handler"
      package     = var.python_standby_zip_path
      memory_size = 512
      timeout     = 30
    }
  }
}
