# Standalone prompt: migrate rolebot from Yandex Cloud to AWS

You are continuing the infrastructure migration for the repository at
`/mnt/c/Users/79169/Desktop/rolebot-hse`.

Read `/mnt/c/Users/79169/Desktop/rolebot-hse/AGENTS.md` first and obey it. Treat
this document as the retained context after the previous conversation has been
cleared. Inspect the repository and current cloud state before making changes;
do not assume this document is a substitute for verification.

## Objective

Migrate the bot backend from Yandex Cloud to AWS while preserving the current
Yandex infrastructure as a working rollback target.

Use approximately the same serverless architecture:

- AWS Lambda instead of Yandex Cloud Functions.
- Amazon SQS FIFO instead of Yandex Message Queue.
- Amazon DynamoDB instead of YDB.
- Amazon S3 instead of Yandex Object Storage where application storage is
  required.
- EventBridge Scheduler instead of the Yandex timer trigger.
- SSM Parameter Store Standard parameters for runtime secrets.
- Cloudflare Workers as a stable ingress/egress and backend-routing layer.

Design for the AWS Free Tier and low-volume operation. Do not claim that zero
cost is guaranteed. Do not create a VPC, NAT Gateway, provisioned Lambda
concurrency, customer-managed KMS key, Secrets Manager secrets, X-Ray tracing,
or another resource with an unnecessary fixed cost.

The production data migration path is deliberately:

```text
YDB -> local canonical JSON dump -> DynamoDB
```

The reverse path must exist before cutover:

```text
DynamoDB -> local canonical JSON dump -> YDB
```

Never destroy, replace, import into the AWS state, or stop managing the existing
Yandex resources. Do not introduce continuous cross-cloud dual writes. Preserve
Yandex as a standby environment and use an explicit maintenance window for the
final transfer.

## Desired architecture

```text
Telegram --+
            +-> Cloudflare routing worker -> selected backend
VK --------+                              +-> Yandex Cloud (rollback)
                                            +-> AWS Lambda (new)

AWS Lambda gateway
  +-> DynamoDB
  +-> SQS FIFO -> ordered worker Lambda
  +-> EventBridge Scheduler -> registration notifier Lambda
  +-> S3 -> upload-completion Lambda
  +-> SSM Parameter Store -> runtime secrets
  +-> Cloudflare egress -> Telegram/VK APIs and shared runtime services
```

Cloudflare is not a network tunnel and is not technically required for AWS to
communicate with VK. Public AWS HTTPS endpoints can receive VK callbacks and
AWS Lambda can make outbound HTTPS requests without a VPC. Keep Cloudflare as
the recommended stable router for both Telegram and VK because it provides one
reversible switch between Yandex and AWS.

## Fixed access and tooling contract

Use the following access methods. Do not substitute static AWS access keys.

### Local AWS inspection and account bootstrap

- Tool: AWS CLI v2. The installed version when this prompt was written was
  `aws-cli/2.37.12`.
- Profile: `rolebot-bootstrap-advanced`.
- Region: `eu-central-1`.
- Account ID: `319215798726`.
- Authentication: AWS CLI browser login backed by the managed account's
  `AccountFullAccessRole`.
- The login session was expired when this file was written. Reauthenticate with:

  ```bash
  aws login --profile rolebot-bootstrap-advanced
  ```

- Verify the target before any AWS operation:

  ```bash
  aws sts get-caller-identity \
    --profile rolebot-bootstrap-advanced \
    --region eu-central-1
  ```

  The returned `Account` must be exactly `319215798726`, and the ARN must be an
  assumed `AccountFullAccessRole` session. Stop if either check fails.

- Always pass both `--profile rolebot-bootstrap-advanced` and
  `--region eu-central-1` to local AWS CLI commands where supported. Do not use
  the incomplete `rolebot-bootstrap` profile.
- Do not create IAM users or access keys. Do not use the root user.
- Use the admin profile for read-only inspection and, only if genuinely needed,
  account-level bootstrap maintenance. Do not use it as the normal application
  deployment mechanism.
- Do not create application resources with ad-hoc `aws ... create-*` commands.
  Application resources belong in Terraform and are deployed through GitHub
  Actions OIDC.

### AWS application deployment from GitHub Actions

- Authentication must use GitHub OIDC through the official
  `aws-actions/configure-aws-credentials` action. Pin the action to a reviewed
  immutable commit SHA when adding it.
- Plan role:
  `arn:aws:iam::319215798726:role/rolebot-github-plan`
- Deploy role:
  `arn:aws:iam::319215798726:role/rolebot-github-deploy`
- GitHub environments:
  - `production-plan` contains `AWS_ROLE_ARN` for the plan role.
  - `production` contains `AWS_ROLE_ARN` for the deploy role.
- GitHub repository variables already exist:
  - `AWS_ACCOUNT_ID=319215798726`
  - `AWS_REGION=eu-central-1`
  - `AWS_TF_STATE_BUCKET=rolebot-terraform-state-319215798726-eu-central-1`
  - `AWS_TF_STATE_KEY=larp-bot/aws-production.tfstate`
- GitHub environment secrets already exist in `production`:
  - `CF_TO_AWS_HMAC_SECRET`
  - `AWS_TO_CF_EGRESS_HMAC_SECRET`
- The existing Yandex workflows set variables named `AWS_ACCESS_KEY_ID` and
  `AWS_SECRET_ACCESS_KEY`, but those values are Yandex Object Storage
  credentials. Never inherit or reuse that job environment for real AWS.
  AWS plan/deploy must be separate jobs or workflows and authenticate only via
  OIDC.
- Use `gh` to inspect GitHub configuration and workflow runs. GitHub CLI 2.98.0
  was authenticated to `ProfessorLayton322/rolebot-hse` when this prompt was
  written. Never print secret values; `gh secret list` is safe because it lists
  names only.

### Terraform

- CI pins Terraform `1.15.9`; use the same version for the new AWS roots.
- Pin the AWS provider to an exact reviewed compatible version and commit the
  generated `.terraform.lock.hcl`.
- Terraform was not installed in the local shell when this prompt was written.
  Use the pinned CI setup or install/use exactly the repository-pinned version
  for local validation; do not silently validate with an unrelated version.
- Keep three independent ownership boundaries:

  ```text
  infra/terraform/       existing Yandex + current Cloudflare resources
  infra/aws-bootstrap/   existing account-level AWS bootstrap resources
  infra/aws/application/ new AWS application resources
  ```

- The existing Yandex Terraform state and AWS application state must never be
  combined.
- AWS application state:

  ```text
  bucket = rolebot-terraform-state-319215798726-eu-central-1
  key    = larp-bot/aws-production.tfstate
  region = eu-central-1
  ```

- The AWS state bucket is already private, versioned, encrypted with S3-managed
  encryption, and uses BucketOwnerEnforced ownership. Do not recreate it.
- If documenting/importing the already-created bootstrap resources into
  Terraform, use a different bootstrap state key. Do not let the application
  deployment role manage the GitHub OIDC provider or its own deployment roles.

### Cloudflare

- Use the repository-pinned invocation exactly:

  ```bash
  npx --yes wrangler@4.125.0 ...
  ```

- Use the existing `scripts/update_cloudflare_secrets.sh` pattern for secret
  injection. Never expose secret values in logs.
- Existing Cloudflare resources are owned by `infra/terraform` and its Yandex
  state. Do not duplicate them in the AWS Terraform state.
- Cloudflare changes therefore continue through the existing Terraform
  deployment workflow; Wrangler is used for the Worker secret bindings.

### Yandex Cloud

- Use `yc` only for read-only inspection. Yandex Cloud CLI 1.28.0 was installed
  when this prompt was written.
- All Yandex infrastructure or deployment mutations must go through the
  existing GitHub Actions Terraform workflow. Direct `yc` mutation is
  forbidden.
- Never destroy the YDB tables, YMQ queues, functions, triggers, API Gateway,
  object storage, service accounts, or rollback configuration.

### Repository delivery

- Preserve the unrelated existing working-tree changes in `.gitignore`,
  `CLAUDE.md`, and `.claude/` unless inspection shows they have since changed or
  become part of the requested work.
- Work only on a non-default branch. Never push directly to `main`.
- Follow `AGENTS.md`: exactly one pull request and one merge start per user
  prompt, using the repository's quiet `gh`-based PR script. After starting the
  merge, end the prompt immediately; do not wait for checks or deployment.
- Always run the repository-pinned `ruff format --check .` before delivery and
  correct relevant formatting failures with the pinned Ruff version.
- Run tests in proportion to the deployment risk.

## AWS bootstrap already completed

Do not repeat this bootstrap blindly. Inspect it first with the fixed AWS CLI
profile.

- AWS account: `319215798726`
- Primary/allowed region: `eu-central-1`
- Terraform state bucket:
  `rolebot-terraform-state-319215798726-eu-central-1`
- Application state key: `larp-bot/aws-production.tfstate`
- GitHub OIDC provider created.
- IAM roles created:
  - `rolebot-github-plan`
  - `rolebot-github-deploy`
- Customer-managed IAM policies created:
  - `rolebot-runtime-iam-management`
  - `rolebot-terraform-plan-state`
- Temporary bootstrap IAM user was deleted.
- IAM Access Analyzer had no findings after bootstrap.

The account's existing managed-account policies were intentionally retained:

- `AdvancedModeRegionRestrictionSecurityControlPolicy`
- `BudgetsSpendLimitDenyBedrockUsage`
- `BudgetsSpendLimitDenyLambdaUsage`
- `BudgetsSpendLimitDenyNewWorkloads`
- `BudgetsSpendLimitDenySageMakerUsage`
- `FullAWSAccess`
- `ManagedAccountSecurityControlPolicy`

Do not remove these merely because their names sound broad or restrictive.
Confirm that the region restriction permits `eu-central-1` before application
deployment. The budget deny policies are managed account guardrails.

## Current application architecture to port

The Yandex stack currently has:

- Five YDB tables: `tg_users`, `vk_users`, `events`, `event_leaders`, and
  `registrations`.
- A FIFO registration-command queue and a standard kick queue.
- A Python gateway function.
- A Python ordered worker.
- A Go registration notifier.
- A Go disk uploader.
- A Python upload-completion function.
- Yandex API Gateway, Object Storage, and an hourly scheduler trigger.
- Cloudflare Telegram ingress and egress workers.

The application bootstrap is currently Yandex-specific: it constructs YDB
repositories and YMQ clients directly. Preserve the domain/application layer
and add AWS adapters behind the existing ports rather than forking business
logic.

YDB transactions currently provide important multi-row guarantees. The
DynamoDB implementation must preserve:

- Delivery deduplication claims.
- Event and leader creation.
- One-to-one Telegram chat binding.
- Confirmation operations that update/delete registrations across events.
- Updates for mutually linked Telegram and VK registrations.
- Registration queue-position swaps.
- Idempotency through operation IDs.

Use DynamoDB transactions and conditional expressions where required. Do not
replace these operations with unrelated eventually consistent writes.

## Target AWS resources

Create the AWS application in disabled/standby mode first.

### DynamoDB

Use five mirrored tables for the simplest reversible migration:

- `larp-bot-prod-tg-users`
- `larp-bot-prod-vk-users`
- `larp-bot-prod-events`
- `larp-bot-prod-event-leaders`
- `larp-bot-prod-registrations`

Use provisioned Standard capacity, not on-demand, and keep aggregate table plus
GSI capacity within the current Free Tier allowance where the workload permits.
Use AWS-owned encryption, deletion protection, and Terraform `prevent_destroy`.
Do not enable paid point-in-time recovery if the requirement is to remain
strictly within free allowances; the validated local canonical dump is the
migration/rollback backup.

Suggested schema rules:

- Store identifiers as DynamoDB strings for lossless JSON round trips.
- Store timestamps as normalized UTC ISO-8601 strings.
- Keep existing opaque JSON fields as strings unless a deliberate schema
  migration is separately tested.
- Preserve the composite keys of `event_leaders` and `registrations`.
- Add only indexes required by current access patterns, such as Telegram handle,
  signup token/chat lookup, and Telegram registration profile lookup.
- Small event-table scans can remain initially if they are already bounded and
  cheaper than unnecessary indexes.

### Lambda and HTTP entry point

- Python runtime: 3.12.
- Go functions: compile `bootstrap` binaries for `provided.al2023`.
- Prefer a Lambda Function URL behind Cloudflare for the lowest fixed cost and
  simplest routing. Preserve HMAC verification and adapt the request/event
  envelope explicitly.
- If an HTTP API is selected instead, document the cost and throttling tradeoff
  and do not assume its introductory free allowance is permanent.
- Do not attach Lambdas to a VPC; they need ordinary outbound internet access
  to VK, Cloudflare, Telegram-related endpoints, Google services, and Yandex
  Disk.
- Set short CloudWatch log retention, such as seven days, and add only essential
  alarms.

### Queues and scheduling

- Use an SQS FIFO queue with a dead-letter queue for registration commands.
- Map the FIFO queue directly to the ordered worker Lambda. The Yandex-specific
  kick queue should not be reproduced unless testing demonstrates a real AWS
  requirement.
- Preserve message-group ordering. With partial batch responses, stop after the
  first failure for a message group and report that message plus subsequent
  unprocessed messages as failed.
- Use EventBridge Scheduler for the hourly notifier.
- Create both the event source mapping and scheduler disabled initially.

### Runtime secrets

- GitHub Actions secrets remain the source of truth during deployment.
- Copy runtime values into SSM Standard `SecureString` parameters under a path
  such as `/larp-bot/production/`.
- Terraform manages parameter names and IAM access but must not receive secret
  values, because that would put them in Terraform state.
- Lambda execution roles read only their necessary parameters. Cache parameter
  values within warm Lambda environments.
- Validate the Google credential JSON against the 4 KB SSM Standard
  parameter limit. If it is larger, use a private encrypted S3 object with
  narrowly scoped IAM access or split it into appropriate parameters.
- Cloudflare egress must temporarily accept both the existing Yandex egress HMAC
  and the new AWS egress HMAC.

## Required implementation sequence

### Phase 0: verify billing guardrails

Inspect rather than assume:

- Free Tier usage alerts enabled.
- CloudWatch billing alerts enabled.
- A small monthly cost budget exists, recommended `$1`, with actual alerts at
  50%, 80%, and 100%, plus a 100% forecast alert.
- The existing managed-account spend-limit policies remain attached.

If console interaction is required for the managed-account budget, explain the
exact clicks to the user. A budget is an alert/guardrail, not a zero-spend
guarantee.

### Phase 1: add isolated AWS infrastructure and CI

1. Add `infra/aws-bootstrap` to document/import the already-created account
   bootstrap without recreating it. Give it a state separate from both Yandex
   and the AWS application.
2. Add `infra/aws/application` for AWS application resources only.
3. Add separate AWS plan/deploy workflows using GitHub OIDC roles.
4. Keep initial application deployment manual through `workflow_dispatch`.
5. Do not run real AWS jobs in the existing Yandex jobs because their
   `AWS_ACCESS_KEY_ID` variables contain Yandex credentials.
6. Update CI to format and validate all Terraform roots using Terraform 1.15.9.
7. Deploy AWS resources in standby mode: no production route, scheduler
   disabled, SQS event mapping disabled, and mutation endpoints disabled.

### Phase 2: add AWS application adapters

1. Implement DynamoDB repositories behind the existing storage ports.
2. Reproduce transactional and conditional behavior with regression tests.
3. Implement SQS FIFO publishing and Lambda batch consumption.
4. Add Function URL/Lambda event handling without breaking the current Yandex
   envelope.
5. Port the Go notifier and uploader away from Yandex metadata/IAM and raw
   Yandex Object Storage assumptions.
6. Implement S3/upload-completion integration.
7. Add active/standby/maintenance runtime gating.
8. Keep existing Yandex paths fully functional and covered by tests.

### Phase 3: build reversible data migration tooling

Add these commands or equivalent clearly named scripts:

```text
scripts/export_ydb_json.py
scripts/import_dynamodb_json.py
scripts/export_dynamodb_json.py
scripts/import_ydb_json.py
```

The canonical document must include:

```json
{
  "schema_version": 1,
  "exported_at": "UTC RFC3339 timestamp",
  "source": "ydb or dynamodb",
  "tables": {
    "tg_users": [],
    "vk_users": [],
    "events": [],
    "event_leaders": [],
    "registrations": []
  },
  "counts": {},
  "hashes": {}
}
```

Migration requirements:

- Deterministic sorting.
- IDs serialized losslessly as strings.
- Timestamps normalized to UTC with consistent precision.
- Primary-key uniqueness checks.
- Leader/registration foreign-key checks against events.
- Existing application model validation.
- DynamoDB 400 KB item-size checks.
- SHA-256 per table and for the complete canonical payload.
- Import order: users, events, leaders, registrations.
- Idempotent and resumable import behavior.
- A DynamoDB re-export followed by count/hash comparison.
- Local dumps stored with restrictive file permissions in a gitignored path.
- Never upload or commit a real user-data dump.

The reverse DynamoDB-to-YDB flow must be implemented and rehearsed before AWS
can accept production writes.

### Phase 4: extend Cloudflare for reversible routing

Add a backend selector with semantics equivalent to:

```text
ACTIVE_BACKEND=yandex|aws|maintenance
YANDEX_BACKEND_URL=...
AWS_BACKEND_URL=...
```

Requirements:

- Retain existing Telegram webhook validation and sender rate limiting.
- Sign Yandex and AWS requests with their separate ingress HMAC secrets.
- Allow Cloudflare egress to validate both Yandex and AWS egress signatures
  during migration.
- Route VK through the same stable Cloudflare layer if feasible, while retaining
  VK callback confirmation behavior.
- Keep the current Yandex Cloudflare runtime-configuration path for rollback
  until AWS is proven.
- Do not transfer Cloudflare resource ownership into AWS Terraform state.

### Phase 5: rehearsal

While Yandex remains production:

1. Export a live YDB snapshot to local canonical JSON.
2. Validate counts, keys, references, types, timestamps, item sizes, and hashes.
3. Import it into rehearsal DynamoDB tables with an unmistakable nonproduction
   prefix.
4. Export those tables back to canonical JSON and compare.
5. Run repository transaction tests against the rehearsal tables.
6. Send synthetic Telegram and VK requests to the standby AWS endpoint.
7. Exercise SQS retry/order behavior, the scheduler target, outbound messages,
   and the upload path without affecting production users.
8. Remove only rehearsal resources after validation. Do not remove the standby
   production stack or anything in Yandex.

### Phase 6: production cutover

Do this only in an agreed maintenance window:

1. Put Cloudflare into maintenance mode.
2. Stop new Yandex mutations using the reversible mode control delivered through
   the existing GitHub Actions Terraform workflow.
3. Disable/no-op the Yandex notifier schedule through the same workflow.
4. Drain both YMQ queues completely, including in-flight messages.
5. Wait for pending upload operations to complete.
6. Create and validate the final local YDB canonical dump.
7. Import it into the production DynamoDB tables.
8. Export DynamoDB and compare counts, keys, and hashes.
9. Enable the AWS SQS event source mapping.
10. Enable the AWS EventBridge schedule.
11. Change AWS from standby to active.
12. Change Cloudflare's active backend to AWS.
13. Confirm VK callback configuration only if VK is not behind Cloudflare.
14. Perform controlled Telegram and VK smoke tests.
15. Verify new DynamoDB records, FIFO processing, scheduled behavior, uploads,
    and outbound delivery.
16. End maintenance mode.

Do not migrate queued YMQ messages. Drain them before the final snapshot so
operations are neither lost nor executed twice.

### Phase 7: rollback

If AWS has accepted no writes:

1. Enter maintenance mode.
2. Switch Cloudflare to Yandex.
3. Re-enable Yandex scheduling and writes through its CI/Terraform path.
4. End maintenance mode.

If AWS has accepted production writes:

1. Enter maintenance mode and stop new AWS mutations.
2. Drain SQS, including in-flight messages.
3. Export DynamoDB to canonical JSON.
4. Validate and import the changes into YDB.
5. Export YDB again and compare counts and hashes.
6. Switch Cloudflare/VK back to Yandex.
7. Re-enable Yandex workers, scheduling, and writes through its CI/Terraform
   path.
8. End maintenance mode.

Never perform a blind route flip after AWS has accepted writes; that would lose
new user activity.

## Validation expectations

At minimum, add or preserve tests for:

- DynamoDB repository mapping and key types.
- Each multi-item transaction and its conditional-failure behavior.
- Idempotent operation IDs and delivery claims.
- Linked Telegram/VK user and registration updates.
- SQS FIFO ordering, retries, and partial-batch failure handling.
- Lambda HTTP event parsing and HMAC verification.
- Cloudflare Yandex/AWS routing and both egress signatures.
- Canonical export determinism and round-trip hashes.
- Migration resumption after partial import.
- Reverse migration into a disposable YDB test target or faithful repository
  test double.
- Existing Yandex adapters and workflows.
- Telegram callback payload byte limits required by `AGENTS.md`, including full
  36-character event UUIDs and supported non-ASCII values whenever buttons are
  affected.

Before delivery, run the repository's pinned checks, including:

```bash
ruff format --check .
ruff check .
mypy
pytest
```

Also run relevant Go tests/vet, Cloudflare tests/typecheck/build, Terraform
format/validate, migration tests, and repository safety scripts for the files
changed.

## Free-tier design references

Recheck current AWS pricing before deployment because allowances can change:

- DynamoDB: <https://aws.amazon.com/dynamodb/pricing/>
- Lambda: <https://aws.amazon.com/lambda/pricing/>
- SQS: <https://aws.amazon.com/sqs/pricing/>
- EventBridge: <https://aws.amazon.com/eventbridge/pricing/>
- SSM Parameter Store: <https://aws.amazon.com/systems-manager/pricing/>
- SSM Standard parameter size:
  <https://docs.aws.amazon.com/systems-manager/latest/APIReference/API_PutParameter.html>
- S3: <https://aws.amazon.com/s3/pricing/>
- API Gateway, if considered: <https://aws.amazon.com/api-gateway/pricing/>

## Starting point for the resumed session

1. Read `AGENTS.md` and this entire file.
2. Inspect `git status` and preserve unrelated work.
3. Reauthenticate the fixed AWS CLI profile if inspection is needed.
4. Verify the AWS identity/account/region and inspect the existing bootstrap
   resources read-only.
5. Inspect GitHub variables, environment secret names, roles, and recent
   workflow state with `gh`.
6. Inspect Yandex read-only with `yc` and Cloudflare with the pinned Wrangler
   version only when needed.
7. Do not redo the completed bootstrap.
8. Continue with the next explicitly requested phase. If the user simply says
   to proceed with the migration, begin with Phase 0 verification and Phase 1,
   keeping all new application resources disabled/standby.
9. Report exact changes, validation performed, remaining manual actions, and
   rollback implications. Follow the one-PR/one-merge-start delivery rule from
   `AGENTS.md`.
