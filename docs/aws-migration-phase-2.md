# AWS migration Phase 2 status

Phase 4 supersedes this document's Function URL isolation detail: the URL now
uses `NONE` so Cloudflare can reach the application-HMAC routes, while the
runtime, SQS consumer, and scheduler remain in standby.

Phase 2 is being delivered in disabled slices so no incomplete runtime can
receive production traffic. Yandex remains production and the AWS application
stack retains all Phase 1 standby controls.

## Application adapter slice

The gateway, ordered worker, notifier, uploader, and upload-completion
functions now have AWS-native composition paths:

- DynamoDB repositories store identifiers as strings and domain records as
  lossless Pydantic JSON. Routine reads use primary keys or the required GSIs;
  table scans are reserved for existing list and lifecycle operations.
- Mutations reuse the established in-memory repository semantics, calculate
  the exact record delta, and commit it with revision conditions in one
  `TransactWriteItems` request. Conflicts reload and retry.
- Event creation commits the event and leaders together. Signup tokens and
  Telegram chat bindings use transactional sentinel records so GSI eventual
  consistency cannot create duplicate bindings.
- Linked confirmations/cancellations, queue-position swaps, operation IDs,
  user delivery claims, and participation-notification claims remain atomic
  and idempotent. Mutations requiring more than DynamoDB's 100-action
  transaction limit fail before any partial write.
- The publisher sends directly to the AWS FIFO queue with event message groups
  and operation-ID deduplication. Lambda batch consumption reports the failed
  item and every later unprocessed item in that FIFO group while allowing
  independent groups to complete.
- Runtime secrets are fetched one at a time from the allowlisted SSM Standard
  parameter paths and cached in warm environments. Non-secret administrator
  role lists remain environment configuration.
- Generated files stage in versioned private S3 and invoke the disk uploader
  asynchronously. Upload-completion accepts either a direct callback payload
  or a batch of private S3 completion objects.
- The Go notifier runs on the AWS Lambda custom runtime, reads lifecycle rows
  from DynamoDB with optimistic revision conditions, publishes scheduled work
  directly to SQS FIFO, reads only its allowlisted SSM parameters, and retains
  the existing notification service and delivery semantics.
- The Go uploader reads immutable staged S3 object versions, verifies SHA-256
  metadata, serializes writes to each Yandex Disk destination with versioned
  S3 locks, and records generation state for idempotent retries. It writes one
  private `upload-completions/<job>.json` object; the S3 notification invokes
  the Python completion function.
- Function URL v2 request method/path fields are accepted without changing the
  Yandex envelope. AWS ingress and egress use their separate HMAC secrets.

The deployable AWS ZIP has its own pinned dependency closure and a build-time
check for Lambda's compressed and uncompressed package limits. YDB-specific
libraries are not imported during AWS startup. The Go AWS builds use separate
`aws.go.mod` lockfiles and Go 1.26.9, so patched AWS binaries do not change the
existing Yandex `golang123` source-module contract.

## Controls that remain disabled

- Function URL authorization after Phase 4: `NONE`, with backend-specific HMAC
  verification and the unchanged standby runtime gate.
- Application mode: `standby`.
- Mutations: disabled.
- SQS event source mapping: disabled.
- EventBridge Scheduler: disabled.
- Cloudflare backend: Yandex.

Even a direct test invocation of any Python or Go function cannot process
application work while this combination is present. The ordered worker returns
every SQS record as failed rather than acknowledging it.

## Next phase

Phase 2 is code-complete but remains deliberately undeployed until this branch
is merged and the manual standby workflow is run again. Phase 3 adds reversible
canonical data migration tooling. Cloudflare dual-signature/routing changes
belong to Phase 4. No standby control should be enabled until the reverse data
path and Phase 5 rehearsal have passed.
