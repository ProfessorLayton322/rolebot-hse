# ADR-001: FIFO draining and secret placement

Status: accepted, 2026-08-21.

Yandex Message Queue FIFO is authoritative for workbook mutations because each event requires strict order. The native Message Queue to Cloud Function trigger currently accepts standard queues only. Therefore publishers persist a FIFO command and then emit a disposable standard kick. A native trigger consumes only the kick and starts a bounded FIFO drainer. The event ID is `MessageGroupId`; operation ID is `MessageDeduplicationId` and is also stored in the participant row.

Cloudflare Worker secret values are installed after Terraform with pinned Wrangler, so Telegram and transport secrets do not enter Terraform state. Yandex Lockbox payload versions are also installed after apply from GitHub's protected environment.

The Yandex Terraform Message Queue resource itself requires an SQS static access key. A dedicated least-privilege YMQ service account and key are Terraform-managed. Consequently that one key exists in encrypted, access-controlled remote tfstate. CI copies it directly to Lockbox; it is not a GitHub Secret or Function environment variable. This is the narrow provider-driven exception to the preferred no-plaintext-in-state rule.
