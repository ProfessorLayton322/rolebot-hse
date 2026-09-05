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

variable "function_package_bucket" {
  type        = string
  description = "Existing private Object Storage bucket used for the Function deployment package."
}

variable "cloudflare_account_id" {
  type = string
}

variable "cloudflare_workers_subdomain" {
  type        = string
  description = "Account workers.dev subdomain, without .workers.dev."
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
  default = 25
  validation {
    condition     = var.webhook_rate_limit_rps >= 1 && var.webhook_rate_limit_rps <= 1000
    error_message = "webhook_rate_limit_rps must be between 1 and 1000."
  }
}

variable "labels" {
  type    = map(string)
  default = {}
}

variable "table_versioning_number" {
  description = "Number of pre-edit statistics workbook backups retained on Yandex Disk."
  type        = number
  default     = 20
  validation {
    condition     = var.table_versioning_number >= 1 && var.table_versioning_number <= 1000 && floor(var.table_versioning_number) == var.table_versioning_number
    error_message = "table_versioning_number must be an integer between 1 and 1000."
  }
}
