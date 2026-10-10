variable "project_name" {
  type        = string
  description = "Short resource-name prefix."
  default     = "larp-bot"
}

variable "yandex_cloud_id" {
  type = string
}

variable "yandex_folder_id" {
  type = string
}

variable "yandex_zone" {
  type    = string
  default = "ru-central1-a"
}

variable "function_zip_path" {
  type        = string
  description = "Deterministic Python function ZIP produced by scripts/build_function.sh."
  default     = "../../dist/function-package.zip"
}

variable "disk_uploader_zip_path" {
  type        = string
  description = "Deterministic Go uploader ZIP produced by scripts/build_disk_uploader.sh."
  default     = "../../dist/disk-uploader.zip"
}

variable "registration_notifier_zip_path" {
  type        = string
  description = "Deterministic Go lifecycle notifier ZIP produced by scripts/build_registration_notifier.sh."
  default     = "../../dist/registration-notifier.zip"
}

variable "function_package_bucket" {
  type        = string
  description = "Existing private, versioned Object Storage bucket used for Terraform state and the Function package."
}

variable "cloudflare_account_id" {
  type = string
}

variable "cloudflare_workers_subdomain" {
  type        = string
  description = "Account workers.dev subdomain, without .workers.dev."
}

variable "cloudflare_active_backend" {
  type        = string
  description = "Backend selected by the stable Cloudflare ingress Worker."
  default     = "yandex"

  validation {
    condition     = contains(["yandex", "aws", "maintenance"], var.cloudflare_active_backend)
    error_message = "cloudflare_active_backend must be yandex, aws, or maintenance."
  }
}

variable "aws_backend_url" {
  type        = string
  description = "HTTPS Lambda Function URL owned by the separate AWS application state."

  validation {
    condition     = can(regex("^https://[^/@?#]+(?::[0-9]+)?(?:/[^?#]*)?$", var.aws_backend_url))
    error_message = "aws_backend_url must be an absolute HTTPS URL without credentials, query, or fragment."
  }
}

variable "yandex_gateway_enabled" {
  type        = bool
  description = "Accept new application work at the Yandex gateway."
  default     = true
}

variable "yandex_background_enabled" {
  type        = bool
  description = "Allow Yandex workers and upload handlers to finish background mutations."
  default     = true
}

variable "yandex_notifier_enabled" {
  type        = bool
  description = "Allow the hourly Yandex notifier invocation to perform work."
  default     = true
}

variable "ydb_rehearsal_enabled" {
  type        = bool
  description = "Create the disposable Phase 5 YDB reverse-migration target."
  default     = false
}

variable "gateway_max_instances" {
  type    = number
  default = 3
  validation {
    condition     = var.gateway_max_instances >= 1 && var.gateway_max_instances <= 20
    error_message = "gateway_max_instances must be between 1 and 20."
  }
}

variable "worker_max_instances" {
  type    = number
  default = 3
  validation {
    condition     = var.worker_max_instances >= 1 && var.worker_max_instances <= 20
    error_message = "worker_max_instances must be between 1 and 20."
  }
}

variable "webhook_rate_limit_rps" {
  type    = number
  default = 10
  validation {
    condition     = var.webhook_rate_limit_rps >= 1 && var.webhook_rate_limit_rps <= 1000
    error_message = "webhook_rate_limit_rps must be between 1 and 1000."
  }
}

variable "telegram_user_rate_limit_per_minute" {
  type        = number
  description = "Telegram updates one user may send per minute before the ingress Worker drops the rest."
  default     = 30
  validation {
    condition     = var.telegram_user_rate_limit_per_minute >= 1 && var.telegram_user_rate_limit_per_minute <= 1000
    error_message = "telegram_user_rate_limit_per_minute must be between 1 and 1000."
  }
}

variable "labels" {
  type    = map(string)
  default = {}
}

variable "google_folder_id" {
  description = "Google Drive folder containing the permanent statistics workbooks."
  type        = string
  validation {
    condition     = length(trimspace(var.google_folder_id)) > 0
    error_message = "google_folder_id must not be empty."
  }
}
