# ADR-001: FIFO draining and secret placement

Status: accepted, 2026-08-21.

Yandex Message Queue FIFO is authoritative for workbook mutations because each event requires strict order. The native Message Queue to Cloud Function trigger currently accepts standard queues only. Therefore publishers persist a FIFO command and then emit a disposable standard kick. A native trigger consumes only the kick and starts a bounded FIFO drainer. The event ID is `MessageGroupId`; operation ID is `MessageDeduplicationId` and is also stored in the participant row.

All application secret values are installed after Terraform as encrypted Cloudflare Worker bindings with pinned Wrangler. Yandex Functions exchange their platform-provided IAM tokens for audience-bound Yandex OIDC ID tokens. The egress Worker verifies the issuer signature, audience, expiry, and exact runtime service-account subjects before returning configuration, which Functions cache for at most 60 seconds. No long-lived authentication secret is placed in a Function environment variable.

The Yandex Terraform Message Queue resource itself requires an SQS static access key. A dedicated least-privilege YMQ service account and key are Terraform-managed. Consequently that one key exists in encrypted, access-controlled remote tfstate. CI copies it directly to the egress Worker; it is not a GitHub Secret or Function environment variable. This is the narrow provider-driven exception to the preferred no-plaintext-in-state rule.
