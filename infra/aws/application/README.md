# AWS application stack — Phase 1 standby

This root owns only the new AWS application resources. It never owns the
existing Yandex or Cloudflare resources and uses the dedicated state object
`larp-bot/aws-production.tfstate` in the pre-existing AWS state bucket.

Phase 1 is intentionally unusable for production traffic:

- the Lambda Function URL uses `AWS_IAM`, so the unsigned Cloudflare Worker
  cannot call it;
- every packaged handler is a non-mutating standby placeholder;
- `APPLICATION_MODE=standby` and `MUTATIONS_ENABLED=false` are fixed in the
  Lambda environment;
- the SQS event source mapping is disabled;
- the EventBridge Scheduler schedule is disabled;
- no Cloudflare route or Yandex resource changes are part of this root.

The five DynamoDB Standard tables and their four GSIs use a total of 9 RCUs and
9 WCUs. Deletion protection and Terraform `prevent_destroy` are enabled, and
paid point-in-time recovery is deliberately disabled. SQS and S3 use AWS-owned
encryption; no customer-managed KMS key, VPC, NAT Gateway, Secrets Manager
secret, X-Ray tracing, or provisioned Lambda concurrency is created.

The manual `AWS standby deploy` workflow is the only application deployment
path. It authenticates with GitHub OIDC, applies this root, then creates or
updates SSM Standard SecureStrings from GitHub environment secrets. Terraform
owns the canonical parameter names and exact IAM ARNs, but deliberately has no
`aws_ssm_parameter` resources: the pinned provider decrypts SecureStrings on
refresh even when a write-only argument is used. Therefore no real or sentinel
value can enter Terraform state or provider memory. The workflow rejects empty
values and values larger than the 4,096-byte Standard parameter limit; the
Google service-account JSON must be split or moved to a private encrypted S3
object before deployment if that check fails.

The workflow also requires an explicit confirmation that the payer-account
budget, Free Tier alerts, and CloudWatch billing alarm in
[`docs/aws-migration-phase-0.md`](../../../docs/aws-migration-phase-0.md) were
completed. Phase 0 inspection found that they cannot be verified or configured
from the managed member account.

Before local validation, build deterministic placeholders:

```bash
bash scripts/build_aws_standby_packages.sh
terraform -chdir=infra/aws/application init -backend=false
terraform -chdir=infra/aws/application validate
```

Current pricing must be rechecked before deployment. Free allowances are usage
limits rather than a promise of zero cost, and data transfer, logs, storage,
requests, or later feature changes can still incur charges.

The official pricing pages were rechecked on 2026-10-10. At that time:

- [DynamoDB Standard provisioned capacity](https://aws.amazon.com/dynamodb/pricing/)
  included 25 RCUs, 25 WCUs, and 25 GB per month;
- [Lambda](https://aws.amazon.com/lambda/pricing/) included one million
  requests and 400,000 GB-seconds per month, and
  [Function URLs](https://docs.aws.amazon.com/lambda/latest/dg/furls-http-invoke-decision.html)
  had no endpoint charge beyond ordinary Lambda usage;
- [SQS](https://aws.amazon.com/sqs/pricing/) included one million requests per
  month;
- [EventBridge Scheduler](https://aws.amazon.com/eventbridge/pricing/) included
  14 million invocations per month;
- [SSM Standard parameters](https://aws.amazon.com/systems-manager/pricing/)
  had no additional storage or standard-throughput interaction charge, with a
  [4 KB value limit](https://docs.aws.amazon.com/systems-manager/latest/userguide/systems-manager-parameter-store.html).

These allowances can change and are shared with other account usage. S3,
CloudWatch, data transfer, overages, and features added later remain potential
costs.
