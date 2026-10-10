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

variable "python_standby_zip_path" {
  description = "Standby-only Python package built by scripts/build_aws_standby_packages.sh."
  type        = string
  default     = "../../../dist/aws-standby-python.zip"
}

variable "go_standby_zip_path" {
  description = "Standby-only Go bootstrap package built by scripts/build_aws_standby_packages.sh."
  type        = string
  default     = "../../../dist/aws-standby-go.zip"
}
