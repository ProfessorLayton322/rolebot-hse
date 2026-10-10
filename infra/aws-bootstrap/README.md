# AWS account bootstrap inventory

This Terraform root is a read-only inventory of bootstrap resources that were
created before the application stack. It deliberately contains only data
sources and checks: it must not recreate, replace, or let the application
deployment role manage the state bucket, GitHub OIDC provider, deployment
roles, or their customer-managed policies.

`plan-policy.json` is the reviewed source document for the existing
`rolebot-terraform-plan-state` customer-managed policy. In addition to state
access, it grants only the configuration reads Terraform needs to refresh the
application resources during pull-request plans. Bootstrap policy maintenance
uses the fixed browser-login administrator profile, never the application
deployment role.

Its backend is isolated at `larp-bot/aws-bootstrap.tfstate`. The application
uses `larp-bot/aws-production.tfstate`, and the existing Yandex/Cloudflare root
continues to use its own Yandex Object Storage state.

Run this inventory only with the browser-login administrator profile:

```bash
aws login --profile rolebot-bootstrap-advanced
aws sts get-caller-identity \
  --profile rolebot-bootstrap-advanced \
  --region eu-central-1
AWS_PROFILE=rolebot-bootstrap-advanced terraform init
AWS_PROFILE=rolebot-bootstrap-advanced terraform plan
```

The caller must be account `319215798726` using an assumed
`AccountFullAccessRole` session. This root is validated in CI with its backend
disabled, but there is intentionally no workflow that applies it.

The AWS provider does not expose read-only data sources for the state bucket's
versioning, ownership-control, public-access-block, or encryption subresources.
Verify those without importing them into this inventory:

```bash
aws s3api get-bucket-versioning --bucket rolebot-terraform-state-319215798726-eu-central-1 --profile rolebot-bootstrap-advanced --region eu-central-1
aws s3api get-bucket-encryption --bucket rolebot-terraform-state-319215798726-eu-central-1 --profile rolebot-bootstrap-advanced --region eu-central-1
aws s3api get-bucket-ownership-controls --bucket rolebot-terraform-state-319215798726-eu-central-1 --profile rolebot-bootstrap-advanced --region eu-central-1
aws s3api get-public-access-block --bucket rolebot-terraform-state-319215798726-eu-central-1 --profile rolebot-bootstrap-advanced --region eu-central-1
```
