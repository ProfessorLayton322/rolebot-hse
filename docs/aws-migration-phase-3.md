# AWS migration Phase 3: reversible database transfer

Phase 3 adds a local, canonical JSON bridge in both directions:

```text
YDB -> local-dumps/*.json -> DynamoDB
DynamoDB -> local-dumps/*.json -> YDB
```

It does not route traffic, enable AWS mutations, change a scheduler, deploy
infrastructure, or read production data by itself. Yandex remains production
and the AWS stack remains in standby.

## Canonical document

Every export contains schema version 1, an RFC3339 UTC export time, its source,
the five logical tables, per-table counts, per-table SHA-256 hashes, and a
complete logical-payload SHA-256 hash. Export metadata is deliberately outside
the payload hash, so a YDB export and its DynamoDB re-export compare exactly.

Rows are validated with the current application models. Numeric account and
chat IDs are canonical decimal strings, all timestamps use six-digit UTC
precision, arrays and tables have deterministic ordering, and registrations
retain their participation-notification delivery markers. DynamoDB revisions,
event uniqueness-lock rows are physical implementation details and are
reconstructed rather than hashed. Leader creation timestamps are retained in
both databases.

Validation rejects:

- duplicate primary keys;
- leaders or registrations whose event is missing;
- duplicate signup tokens or Telegram chat bindings;
- invalid application records, timestamps, IDs, counts, or hashes;
- a physical DynamoDB item over 400 KiB;
- missing, extra, or incorrect DynamoDB uniqueness locks and index projections.

## Local file safety

`local-dumps/` is gitignored. The commands accept dump paths only inside that
directory, create directories as `0700`, create files as `0600`, refuse to
overwrite an existing dump, and reject an import file that has group/other
permission bits. They abort before writing user data if the filesystem cannot
enforce those modes. In particular, a WSL-mounted Windows filesystem may need
DrvFs metadata enabled; do not weaken this check or copy a real dump into the
repository history.

Never print, upload, attach, or commit a real dump.

## Authentication

YDB commands require `YDB_ENDPOINT`, `YDB_DATABASE`, and `YDB_IAM_TOKEN`.
Obtain the short-lived token without printing it:

```bash
export YDB_IAM_TOKEN="$(yc iam create-token)"
```

DynamoDB commands always use AWS CLI profile
`rolebot-bootstrap-advanced`, region `eu-central-1`, and account
`319215798726`. Before touching a table they call STS and require an assumed
`AccountFullAccessRole` session. Reauthenticate when necessary:

```bash
aws login --profile rolebot-bootstrap-advanced
```

No static AWS keys are accepted.

## Commands

Export YDB:

```bash
python3 scripts/export_ydb_json.py \
  --output local-dumps/ydb-YYYYMMDDTHHMMSSZ.json
```

Import into a rehearsal or production-prefix DynamoDB target:

```bash
python3 scripts/import_dynamodb_json.py \
  --input local-dumps/ydb-YYYYMMDDTHHMMSSZ.json \
  --table-prefix larp-bot-rehearsal
```

The production prefix is `larp-bot-prod` and is the default. Use an explicit,
unmistakable rehearsal prefix in Phase 5. The importer processes users, events,
leaders, then registrations. Matching records are skipped, differing records
are conditionally revised, and a stopped import can be rerun. After every
import it re-exports the target and compares all counts and hashes.

Export DynamoDB:

```bash
python3 scripts/export_dynamodb_json.py \
  --table-prefix larp-bot-prod \
  --output local-dumps/dynamodb-YYYYMMDDTHHMMSSZ.json
```

Reverse-import into YDB:

```bash
python3 scripts/import_ydb_json.py \
  --input local-dumps/dynamodb-YYYYMMDDTHHMMSSZ.json
```

An importer refuses to proceed if the target has keys absent from the dump.
During an agreed maintenance window, the following paired flags turn the
operation into an exact synchronization and remove those extra keys in reverse
dependency order:

```text
--delete-extra --confirm-delete-extra DELETE-EXTRA-DURING-MAINTENANCE
```

Exact synchronization is required for rollback when records were deleted after
AWS began accepting writes. It must not be used against a live writable target.

The reverse path has automated coverage through a stateful YDB repository test
double. Phase 5 must still rehearse it against a disposable YDB target before
AWS is permitted to accept production writes.
