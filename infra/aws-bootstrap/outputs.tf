output "bootstrap_inventory" {
  description = "Identifiers of the existing, separately owned AWS bootstrap resources."
  value = {
    account_id                    = data.aws_caller_identity.current.account_id
    region                        = data.aws_region.current.region
    terraform_state_bucket        = data.aws_s3_bucket.terraform_state.id
    github_oidc_provider_arn      = data.aws_iam_openid_connect_provider.github.arn
    github_plan_role_arn          = data.aws_iam_role.github_plan.arn
    github_deploy_role_arn        = data.aws_iam_role.github_deploy.arn
    runtime_iam_management_policy = data.aws_iam_policy.runtime_iam_management.arn
    terraform_plan_state_policy   = data.aws_iam_policy.terraform_plan_state.arn
  }
}
