# Registration DLQ recovery — 2026-11-15 investigation

## Cause and recovery

The `larp-bot-prod-registration-dlq-not-empty` alarm was caused by one VK
`ENLIST` command exhausting its five delivery attempts. CloudWatch recorded
failures between 2026-11-14 20:31:32 and 20:43:31 UTC while notifying a game
master through the Cloudflare Telegram egress. Four attempts raised
`httpx.ReadTimeout`; one returned HTTP 502. These observations establish an
external delivery-path failure, but do not identify whether Telegram or
Cloudflare caused the underlying outage.

The signup mutation precedes notification delivery. Existing operation and
notification delivery markers allow the worker to retry persisted commands.
The incident did not require changing application code or AWS resources.

An authenticated `getMe` call through the same egress succeeded before replay.
An SQS message move task then redrove the single message to its original FIFO
queue at one message per second. AWS reported the task completed with one of
one messages moved. The worker logged `ordered_command_completed` for
operation `b7b69841-1cd6-5176-8c3d-f7ebcdae0382` at
2026-11-14 21:12:23 UTC. Both queues subsequently reported zero visible and
zero in-flight messages. No messages were purged or discarded.

Validation: all 94 tests in `test_master_participation_notifications.py`,
`test_deferred_transports.py`, and `test_worker.py` passed; pinned Ruff
0.16.4 reported all 197 files formatted.

## Recovery procedure

1. Authenticate with the production profile and verify the STS account identity.
   Pass `--profile rolebot-bootstrap-advanced --region eu-central-1` explicitly
   for production CLI calls.
2. Read alarm history, queue counts, and worker errors. Inspect DLQ messages
   without deleting them; keep message bodies and credentials out of incident
   documents. A receive temporarily changes visibility, so allow that timeout
   to expire before redriving.
3. Establish that the failure is resolved before replay. For Telegram egress
   outages, an authenticated `getMe` checks the route without sending a message.
   Inspect operation and delivery-marker behavior before replaying mutations.
4. Use `sqs start-message-move-task` from the DLQ to the original source queue
   with a bounded rate. Do not purge the DLQ or manually acknowledge failed work.
   Resource changes remain subject to the repository's zero-cost requirement.
5. Verify task completion, a successful worker completion for every recovered
   operation, and zero visible, delayed, and in-flight messages in both queues.
   CloudWatch alarm state can lag behind recovery until the next metric period;
   do not force the alarm state to OK.
