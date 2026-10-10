variable "aws_account_id" {
  description = "AWS account that owns the pre-existing bootstrap resources."
  type        = string
  default     = "319215798726"

  validation {
    condition     = var.aws_account_id == "319215798726"
    error_message = "The rolebot bootstrap inventory is pinned to account 319215798726."
  }
}

variable "aws_region" {
  description = "Only AWS region approved for the rolebot workload."
  type        = string
  default     = "eu-central-1"

  validation {
    condition     = var.aws_region == "eu-central-1"
    error_message = "The rolebot workload is restricted to eu-central-1."
  }
}

variable "github_repository" {
  description = "Repository trusted by the existing GitHub OIDC roles."
  type        = string
  default     = "ProfessorLayton322/rolebot-hse"
}
