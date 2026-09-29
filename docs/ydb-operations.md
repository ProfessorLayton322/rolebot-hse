# YDB operation spike investigation, 2026-09-29

Two separate problems caused the apparent regression after PR #155: the
dashboard exaggerated sparse RU samples, and linked-account lookups still
scanned the Telegram user table. Reducing the number of SQL statements alone
did not address either problem.

## Dashboard overcounting

The existing `ydb-operations-dash` dashboard integrated
`api.units.consumed_per_second`. This is a sparse gauge: during idle periods
it emits a zero after a burst, then omits samples until the next burst.
Monium's `integral()` multiplied the next nonzero value by the time since the
previous sample, including the idle gap. Switching the lifecycle timer from
one minute to five minutes reduced actual work but lengthened those gaps.

Read-only replay of production metrics, with downsampling disabled:

| UTC interval | Old integral | Corrected integral | SQL executions |
| --- | ---: | ---: | ---: |
| Sep 29, 08:00–08:16 | 215.98 RU | 18.00 RU | 3 |
| Sep 28, 12:00–Sep 29, 08:50 | 141,421.60 RU | 48,571.39 RU | 2,602 |

For example, at Sep 28 21:33:15 UTC, the old cumulative graph jumped by
35,270.49 RU after a 195-second sample gap. The corresponding 15-second
sample accounted for 2,713.11 RU. These are monitoring estimates, not a
billing reconciliation.

`infra/terraform/monitoring.tf` adopts the existing dashboard by ID and updates
it through Terraform CI/CD. The RU calculation adds zero times the continuous
`api.grpc.request.count` series for `ExecuteDataQuery` before integration.
This aligns the sparse gauge to the regular 15-second observation grid without
adding request counts to RU. A separate chart shows SQL execution counts.

Keep downsampling disabled for these calculations. Averaging a sparse gauge
before integration also overstates usage: a replay of the same idle interval
with a 60-second average produced approximately 48 RU instead of 18 RU.
`replace_nan()` alone does not repair missing timestamps. The continuous
request series is needed as well.

## Actual read amplification

The retained six-hour `.sys/query_metrics_one_minute` statistics inspected
during the investigation showed:

| Query | Calls | Rows read |
| --- | ---: | ---: |
| Telegram reciprocal-link owner check | 138 | 17,802 |
| Reverse lookup of a Telegram profile from VK | 6 | 774 |
| Combined lifecycle scheduler | 73 | 438 |

The identity queries filtered `tg_users` by `telegram_handle` and
`linked_vk_id`, but its primary key is `tg_id`. Every owner check read all
129 profiles. `LIMIT 2` limits results; it does not eliminate the table scan.
These checks recur during role checks and menu navigation.

The synchronous `telegram_identity` index orders keys by
`(telegram_handle, linked_vk_id)`. All three identity lookup paths explicitly
use it through `VIEW telegram_identity`. The handle prefix also supports
handle-only lookup. The owner check can read matching index entries without
fetching entire user profiles. Cost now depends on matches rather than the
entire user population, with some added index maintenance cost on writes.

The index is deliberately non-unique: existing ambiguous links must remain
valid stored data and must still be rejected by identity resolution. The
two-match limit, owner-ID check, and reciprocal VK-handle check are retained.
Synchronous maintenance prevents stale index entries from conferring access.
Functions using these lookups depend on index creation completing before
their new version is deployed.

## Verification and future diagnosis

Regression tests cover zero, one, and two matches; a different Telegram owner;
nonreciprocal VK handles; explicit index use; and Terraform deployment ordering.
The dashboard formulas were replayed against production metrics without
changing production resources. The dashboard import plan showed an in-place
update with no deletion. Production index performance must be checked after
the GitHub Actions deployment; this task stops when the PR merge is started.

For future incidents, inspect RPC counts and statuses separately from RU, then
use a bounded, read-only query against YDB's system statistics:

```sql
SELECT QueryText,
       SUM(Count) AS Calls,
       SUM(SumReadRows) AS RowsRead,
       SUM(SumReadBytes) AS BytesRead,
       SUM(SumUpdateRows) AS RowsWritten
FROM `.sys/query_metrics_one_minute`
GROUP BY QueryText
ORDER BY RowsRead DESC
LIMIT 20;
```

Do not use statement counts alone to assess an optimization. Check rows read,
query cost, idle sampling gaps, and dashboard downsampling over the same fixed
time interval. System statistics have limited retention and may be incomplete
under heavy load.

References: [YDB system views](https://ydb.tech/docs/en/dev/system-views),
[secondary indexes](https://ydb.tech/docs/en/dev/secondary-indexes), and
[Monitoring metric reads](https://yandex.cloud/en/docs/monitoring/api-ref/MetricsData/read).
