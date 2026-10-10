# AWS application stack — Cloudflare-routable standby runtime

This root owns only the new AWS application resources. It never owns the
existing Yandex or Cloudflare resources and uses the dedicated state object
`larp-bot/aws-production.tfstate` in the pre-existing AWS state bucket.

The stack remains intentionally unusable for production traffic:

- the Lambda Function URL uses `NONE` so Cloudflare can reach it, but Telegram
  and VK use only the backend-specific HMAC-authenticated application routes;
- all Python and Go functions contain AWS-native adapters, but their runtime
  gates reject application work unless both `APPLICATION_MODE=active` and
  `MUTATIONS_ENABLED=true`;
- `APPLICATION_MODE=standby` and `MUTATIONS_ENABLED=false` are fixed in the
  Lambda environment;
- the SQS event source mapping is disabled;
- the EventBridge Scheduler schedule is disabled;
- no Cloudflare or Yandex resource is owned by this root.

The URL becoming public does not activate AWS. The fixed standby gate returns
503 before constructing the application, and the queue consumer and schedule
remain disabled. The existing Yandex/Cloudflare Terraform root owns the Worker
code and installs `AWS_BACKEND_URL`; a later maintenance-window change selects
AWS only after the data transfer and runtime activation steps are complete.

The five DynamoDB Standard tables and their five GSIs use a total of 10 RCUs and
10 WCUs. Deletion protection and Terraform `prevent_destroy` are enabled, and
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

The Python package uses a separate pinned dependency closure that omits YDB,
gRPC, and the AWS SDK already supplied by the Lambda Python runtime. The two Go
packages compile as AWS Lambda `provided.al2023` bootstrap binaries with their
AWS SDK dependencies linked in. Separate `aws.go.mod` files let CI build those
binaries with pinned Go 1.26.9 without raising the module requirement of the
Yandex `golang123` rollback functions. The build enforces Lambda package-size
limits.

Before local validation, build deterministic artifacts:

```bash
bash scripts/build_aws_application_packages.sh
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
