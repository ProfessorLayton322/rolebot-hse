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
