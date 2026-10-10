variable "aws_account_id" {
  description = "Fixed AWS account for the production standby stack."
  type        = string
  default     = "319215798726"

  validation {
    condition     = var.aws_account_id == "319215798726"
    error_message = "The rolebot application is pinned to account 319215798726."
  }
}

variable "aws_region" {
  description = "Only AWS region approved for the production standby stack."
  type        = string
  default     = "eu-central-1"

  validation {
    condition     = var.aws_region == "eu-central-1"
    error_message = "The rolebot application is restricted to eu-central-1."
  }
}

variable "project_name" {
  description = "Resource name prefix."
  type        = string
  default     = "larp-bot"
}

variable "environment" {
  description = "Deployment environment suffix."
  type        = string
  default     = "prod"
}

variable "python_application_zip_path" {
  description = "Gated Python application package built by scripts/build_aws_application_packages.sh."
  type        = string
  default     = "../../../dist/aws-application-python.zip"
}

variable "registration_notifier_zip_path" {
  description = "AWS-native Go registration notifier package."
  type        = string
  default     = "../../../dist/aws-registration-notifier.zip"
}

variable "disk_uploader_zip_path" {
  description = "AWS-native Go disk uploader package."
  type        = string
  default     = "../../../dist/aws-disk-uploader.zip"
}

variable "cloudflare_workers_subdomain" {
  description = "Existing Cloudflare workers.dev account subdomain."
  type        = string
}

variable "google_folder_id" {
  description = "Existing Google Drive folder used by statistics exports."
  type        = string
}

variable "tg_admin_ids" {
  description = "JSON array of Telegram administrators."
  type        = string
}

variable "vk_admin_ids" {
  description = "JSON array of VK administrators."
  type        = string
}

variable "tg_gamemaster_ids" {
  description = "JSON array of Telegram gamemasters."
  type        = string
}

variable "vk_gamemaster_ids" {
  description = "JSON array of VK gamemasters."
  type        = string
}

variable "application_mode" {
  description = "AWS runtime mode. Production work is accepted only in active mode."
  type        = string
  default     = "standby"

  validation {
    condition     = contains(["standby", "active", "maintenance"], var.application_mode)
    error_message = "application_mode must be standby, active, or maintenance."
  }
}

variable "mutations_enabled" {
  description = "Allow the AWS runtime to mutate application state."
  type        = bool
  default     = false
}

variable "sqs_event_mapping_enabled" {
  description = "Enable the ordered-worker SQS event source mapping."
  type        = bool
  default     = false
}

variable "scheduler_enabled" {
  description = "Enable the hourly EventBridge registration notifier schedule."
  type        = bool
  default     = false
}

variable "rehearsal_tables_enabled" {
  description = "Create temporary larp-bot-rehearsal DynamoDB tables for Phase 5."
  type        = bool
  default     = false
}

variable "runtime_table_target" {
  description = "DynamoDB table set used by the AWS Lambdas."
  type        = string
  default     = "production"

  validation {
    condition     = contains(["production", "rehearsal"], var.runtime_table_target)
    error_message = "runtime_table_target must be production or rehearsal."
  }
}

check "safe_runtime_controls" {
  assert {
    condition     = !var.mutations_enabled || var.application_mode == "active"
    error_message = "mutations_enabled requires application_mode=active."
  }

  assert {
    condition     = !var.sqs_event_mapping_enabled || var.mutations_enabled
    error_message = "the SQS event mapping requires mutations_enabled=true."
  }

  assert {
    condition     = !var.scheduler_enabled || var.mutations_enabled
    error_message = "the scheduler requires mutations_enabled=true."
  }

  assert {
    condition     = var.runtime_table_target != "rehearsal" || var.rehearsal_tables_enabled
    error_message = "runtime_table_target=rehearsal requires rehearsal_tables_enabled=true."
  }
}
