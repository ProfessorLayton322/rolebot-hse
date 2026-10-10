# AWS migration Phase 0 verification

Verified read-only on 2026-10-10 with AWS CLI v2.37.12, profile
`rolebot-bootstrap-advanced`, and the `eu-central-1` workload region.

## Verified bootstrap and account state

- Caller identity is account `319215798726` through an assumed
  `AccountFullAccessRole` session.
- `eu-central-1` is enabled.
- The Terraform state bucket is in `eu-central-1`, versioned, private,
  `BucketOwnerEnforced`, encrypted with S3-managed `AES256`, and contains no
  `larp-bot/` state object yet.
- The GitHub OIDC provider has the `sts.amazonaws.com` audience.
- `rolebot-github-plan` trusts only the `production-plan` environment and has
  `ViewOnlyAccess` plus state read/lock access.
- `rolebot-github-deploy` trusts only the `production` environment and has
  `PowerUserAccess` plus narrowly named `larp-bot-*` IAM management and
  `PassRole` permissions.
- No DynamoDB tables, Lambda functions, SQS queues, EventBridge Scheduler
  schedules, or SSM parameters exist in `eu-central-1`. The state bucket is the
  account's only S3 bucket.
- The managed-account Budgets Spend Limit service role remains present. The
  member account cannot enumerate organization service-control policies, so the
  centrally managed policy names from the migration handoff must remain
  untouched and must be checked by the payer-account administrator.
- IAM Access Analyzer returned no analyzer in `eu-central-1`; consequently the
  earlier point-in-time “no findings” result is not a continuously monitored
  assertion.

GitHub repository variables, the two OIDC role variables, and the production
secret names required by the AWS workflow were also verified. Yandex remains
active: its five Functions, both triggers, YDB database, deletion protection,
and versioned Object Storage bucket were observed read-only. Cloudflare was not
changed; the local Wrangler session had expired, and Phase 1 does not depend on
a Cloudflare change.

## Billing guardrail completion and first standby deployment

The payer-account administrator confirmed on 2026-10-10 that the alert setup
below was completed. The managed member account still cannot independently
enumerate those payer-owned settings, so this is an operator attestation rather
than a member-account API assertion.

After that confirmation, GitHub Actions run
[`38013678685`](https://github.com/ProfessorLayton322/rolebot-hse/actions/runs/38013678685)
successfully applied the Phase 1 standby stack and populated its Standard
SecureString parameters. The workflow then asserted that the Function URL used
`AWS_IAM`, the SQS event source mapping was disabled, and the EventBridge
schedule was disabled. Yandex and Cloudflare routing were not changed.

## Payer-account procedure used

The workload account has no account-local AWS Budget and no CloudWatch alarm
for the `AWS/Billing` namespace in `us-east-1`. The managed account denies the
`GetBillingPreferences` API for `BILLING_ALERTS`, and the Free Tier plan API has
no member-account data. Those settings therefore require the payer/management
account console and cannot be safely inferred or repaired with the application
deployment role.

The following is the retained procedure for future audits or replacement alert
destinations:

1. Sign in to the AWS payer/management account and open **Billing and Cost
   Management**.
2. Open **Billing preferences** (under **Preferences** if the navigation is
   collapsed). In **Alert preferences**, enable **Receive AWS Free Tier usage
   alerts** and **Receive CloudWatch billing alerts**, enter the monitored email
   address, and save. If either control is locked, the managed-account
   administrator must perform this step.
3. Open **Budgets** → **Create budget** → **Customize (advanced)** → **Cost
   budget**. Choose a recurring monthly, fixed budget of **USD 1.00**. Add
   actual-cost alerts at **50%**, **80%**, and **100%**, plus a forecasted-cost
   alert at **100%**. Attach a verified email recipient or centrally managed SNS
   topic. Do not attach an automatic destructive action.
4. Switch the console region to **US East (N. Virginia) / us-east-1**, because
   AWS billing metrics live there. Open **CloudWatch** → **Alarms** → **All
   alarms** → **Create alarm** → **Select metric** → **Billing** → **Total
   Estimated Charge** → **USD**. Create a greater-than **USD 1.00** alarm with a
   verified SNS/email notification. The billing metric can take several minutes
   to appear after the preference is enabled.
5. Confirm the centrally managed policies named in `AWS_MIGRATION.md` remain
   attached and that `AdvancedModeRegionRestrictionSecurityControlPolicy`
   permits `eu-central-1`.

A budget and alarms are warnings/guardrails, not a guarantee of zero spend.
The application workflow remains manual, and its Phase 1 configuration is
hard-disabled even after apply.
