terraform {
  required_version = "= 1.15.9"

  backend "s3" {
    bucket       = "rolebot-terraform-state-319215798726-eu-central-1"
    key          = "larp-bot/aws-bootstrap.tfstate"
    region       = "eu-central-1"
    encrypt      = true
    use_lockfile = true
  }

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "6.68.0"
    }
  }
}

provider "aws" {
  region              = var.aws_region
  allowed_account_ids = [var.aws_account_id]
}
