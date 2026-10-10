data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}
data "aws_region" "current" {}

data "aws_s3_bucket" "terraform_state" {
  bucket = "rolebot-terraform-state-${var.aws_account_id}-${var.aws_region}"
}

data "aws_iam_openid_connect_provider" "github" {
  arn = "arn:${data.aws_partition.current.partition}:iam::${var.aws_account_id}:oidc-provider/token.actions.githubusercontent.com"
}

data "aws_iam_role" "github_plan" {
  name = "rolebot-github-plan"
}

data "aws_iam_role" "github_deploy" {
  name = "rolebot-github-deploy"
}

data "aws_iam_policy" "runtime_iam_management" {
  name = "rolebot-runtime-iam-management"
}

data "aws_iam_policy" "terraform_plan_state" {
  name = "rolebot-terraform-plan-state"
}

check "fixed_account_and_region" {
  assert {
    condition = (
      data.aws_caller_identity.current.account_id == var.aws_account_id &&
      data.aws_region.current.region == var.aws_region
    )
    error_message = "Refusing to inspect bootstrap resources outside the fixed account and region."
  }
}

check "state_bucket_identity" {
  assert {
    condition     = data.aws_s3_bucket.terraform_state.id == "rolebot-terraform-state-${var.aws_account_id}-${var.aws_region}"
    error_message = "The bootstrap inventory resolved an unexpected state bucket."
  }
}

check "github_oidc_audience" {
  assert {
    condition     = contains(data.aws_iam_openid_connect_provider.github.client_id_list, "sts.amazonaws.com")
    error_message = "The existing GitHub OIDC provider must trust the STS audience."
  }
}
