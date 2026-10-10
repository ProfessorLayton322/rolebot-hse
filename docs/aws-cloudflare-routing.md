# Reversible Cloudflare backend routing

Phase 4 keeps both public bot callbacks on the existing
`larp-bot-telegram-ingress` Worker and leaves Yandex selected. Cloudflare
resource ownership remains in `infra/terraform`; the AWS Terraform state owns
only the public, HMAC-protected Lambda Function URL.

## Runtime selector

The production deployment maps these GitHub repository variables and Terraform
outputs to non-secret Worker configuration bindings:

```text
ACTIVE_BACKEND=yandex|aws|maintenance
YANDEX_BACKEND_URL=https://<api-gateway-host>
AWS_BACKEND_URL=https://<function-url-host>/
```

`ACTIVE_BACKEND` must be one of the three exact lowercase values. An absent or
invalid selector fails closed with HTTP 503. Backend URLs must be absolute HTTPS
URLs without embedded credentials. The Worker replaces their paths with the
fixed application routes and never accepts a caller-supplied destination.

- `yandex` forwards Telegram and VK to Yandex using
  `CF_TO_YANDEX_HMAC_SECRET`.
- `aws` forwards them to the Lambda Function URL using
  `CF_TO_AWS_HMAC_SECRET`.
- `maintenance` validates and acknowledges callbacks without forwarding
  application work. VK server confirmation is still answered locally after
  checking the VK group and callback secret.

Telegram keeps its webhook-secret check, body limit, sender rate limit, inline
reply contract, and timeout behavior. VK uses the same stable Worker at
`https://larp-bot-telegram-ingress.<workers-subdomain>.workers.dev/vk`; the
Worker validates the VK callback credentials before forwarding to the signed
`/webhooks/vk-cloudflare` backend route. The original direct Yandex
`/webhooks/vk` route remains available only as a rollback target. AWS rejects
that unsigned direct route.

Outbound Telegram calls accept either
`YANDEX_TO_CF_EGRESS_HMAC_SECRET` or `AWS_TO_CF_EGRESS_HMAC_SECRET` during the
migration. The OIDC-protected Yandex runtime-configuration response continues
to expose only the Yandex secret; AWS reads its secret from SSM Parameter Store.

## Safe switch order

Do not select AWS merely because the URL is reachable. During Phase 4 the
Lambda runtime remains in `standby`, mutations remain disabled, the SQS event
mapping is disabled, and the EventBridge schedule is disabled.

For a cutover or rollback with writes, follow `AWS_MIGRATION.md`: enter
maintenance, stop and drain the active backend, transfer and verify the
canonical database dump, activate the destination runtime, change the selector,
and only then leave maintenance. A route flip without the data-transfer steps
is safe only when AWS has accepted no writes.

To change only the Worker selector through the existing deployment path:

1. Set the repository variable `ACTIVE_BACKEND` to the intended value.
2. Run **Deploy production**, which reapplies the Yandex/Cloudflare root and
   reinjects Worker bindings with pinned Wrangler `4.125.0`.
3. For VK, configure the emitted `VK callback` URL once. Confirmation remains
   stable across all selector values.

HMACs and VK credentials remain encrypted Worker secrets injected by the
pinned Wrangler command. Never put either HMAC secret in a Terraform variable
or state. Never move the Cloudflare Worker resources into
`infra/aws/application`.
