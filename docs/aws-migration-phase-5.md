# AWS migration Phase 5 rehearsal record

Date: 2026-10-10

Phase 5 was completed while Yandex remained the active production backend. No
production maintenance window or Phase 6 route switch was started.

## Data rehearsal

The live YDB data was exported to canonical JSON in the private local
`~/rolebot-local-dumps` directory. The files have mode `0600` and are excluded
from the repository.

The validated snapshot contained:

| Table | Rows |
| --- | ---: |
| `tg_users` | 165 |
| `vk_users` | 28 |
| `events` | 13 |
| `event_leaders` | 22 |
| `registrations` | 190 |

The canonical payload hash was
`c7d35f2e58c6010871bfb5e19e21b31153d103955738d8a65279c4564eb29edf`.
The following comparisons reproduced the same row counts and canonical payload
hash:

1. YDB export -> rehearsal DynamoDB import -> DynamoDB export.
2. DynamoDB export -> temporary rehearsal YDB import -> YDB export.
3. DynamoDB export after the rehearsal repository transaction tests.

The local evidence files are:

- `phase5-ydb-20261010T070000Z.json`
- `phase5-dynamodb-rehearsal-20261010T072100Z.json`
- `phase5-ydb-reverse-rehearsal-20261010T072400Z.json`
- `phase5-dynamodb-post-transactions-20261010T072700Z.json`

## Integration rehearsal

The rehearsal verified:

- DynamoDB repository transactions against the isolated rehearsal tables;
- SQS FIFO ordering and retry behavior, followed by an empty queue;
- signed synthetic Telegram and VK requests against the AWS gateway;
- a read-only AWS-signed `getMe` request through Cloudflare egress;
- the disabled EventBridge target's no-op behavior;
- S3 staging, the Go disk uploader, a temporary Yandex Disk upload, and cleanup;
- cleanup of synthetic DynamoDB, S3, upload-lock, and Yandex Disk artifacts.

GitHub Actions run `38025752269` completed the final AWS integration rehearsal.
The rehearsal exposed and fixed missing versioned-S3 lock permissions and the
need for the uploader to distinguish absent state objects.

## Cleanup and resulting state

AWS cleanup run `38025969481` removed the five rehearsal DynamoDB tables and
restored the application to standby with mutations, SQS event consumption, and
the scheduler disabled. Runtime table names point to the production tables,
which remain empty pending cutover.

The Yandex Terraform apply in run `38025971431` removed the temporary rehearsal
YDB database. A transient Cloudflare secret-propagation response caused that
run's immediate smoke check to fail after the successful apply. The smoke test
now waits for the secret-backed edge authentication layer. Run `38030406430`
then completed the Yandex deployment and full production smoke test
successfully.

The resulting safe state is:

- Cloudflare active backend: `yandex`;
- Yandex gateway, background processing, and notifier: enabled;
- production YDB: running with deletion protection;
- AWS application: standby, mutations disabled;
- AWS SQS event source mapping and EventBridge schedule: disabled;
- temporary AWS and Yandex rehearsal databases: removed.

## Phase 6 manual gate

Phase 6 has not started. The configured VK access token returns VK API error 15
for `groups.getCallbackServers`, so automation cannot prove that VK is using the
stable Cloudflare callback before disabling Yandex writes.

Before scheduling cutover, a VK community administrator must:

1. Open community management -> API usage -> Callback API and select the
   community's callback server.
2. Set its URL exactly to
   `https://larp-bot-telegram-ingress.fbykoff.workers.dev/vk`.
3. Keep the existing callback secret, confirm the server, and ensure
   `message_new` events are enabled.
4. Report that the server is confirmed; do not send the callback secret.

Only after that confirmation and agreement on a maintenance window should the
Phase 6 maintenance, final snapshot/import, queue activation, and route switch
be executed.
