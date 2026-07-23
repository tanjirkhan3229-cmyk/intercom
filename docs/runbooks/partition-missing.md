# Runbook — Monthly partition pre-create canary failed

**Severity:** SEV-2 (writes to the next month's partition will fail if not fixed before month-end).

## Alert name
`PartitionMissing`

## Metric / expression
Not a `relay_*` series — driven by the DB canary function. High-volume tables (`events`, `sends`, `message_events`, `parts`/threads, webhook/audit logs) are `PARTITION BY RANGE (created_at)` with monthly partitions pre-created **T+2 months ahead** by `relay_ensure_partitions(parent, n)`, run from a `housekeeping` Celery task; `relay_missing_partitions` drives the alert (RFC-002 §5.3).

```sql
-- alert when the canary reports any parent short of its T+2 buffer.
-- relay_missing_partitions(parent, months_ahead) is per-parent and REQUIRES both
-- args; run it once per partitioned parent (the same set the callsites drive):
SELECT relay_missing_partitions('events', 2);
SELECT relay_missing_partitions('message_events', 2);
SELECT relay_missing_partitions('conversation_parts', 2);
SELECT relay_missing_partitions('webhook_deliveries', 2);
```

Corroborate from metrics: the housekeeping task failing shows as `relay_celery_tasks_total{task=~".*partition.*|.*ensure.*", status="failure"}`.

## Symptom / user impact
No immediate user impact **if** caught early — it is a leading canary. If a future month's partition is never created, inserts dated into that month (W3 events append ~600 rows/s, W5 sends bursts) will **fail** once the clock rolls over, taking down event ingestion / send logging for that period.

## Dashboards to open
- **Housekeeping / beat** — did the pre-create task run on schedule? `relay_celery_tasks_total` for the partition task.
- **DB** — `relay_missing_partitions('<parent>', 2)` output per parent (`events`, `message_events`, `conversation_parts`, `webhook_deliveries`); existing partitions per parent.
- **Beat schedule** — is the scheduler alive and firing the housekeeping job?

## Diagnosis steps
1. Which parent tables are short? Query `relay_missing_partitions('<parent>', 2)` per parent (`events`, `message_events`, `conversation_parts`, `webhook_deliveries`) — it takes two args and is per-parent.
2. Did the `housekeeping` task run and fail, or not run at all (beat down)? Check task metrics and beat health.
3. Failure cause: DDL lock/timeout (the migration wrapper applies `lock_timeout='2s'` / `statement_timeout='30s'`), permissions (partition DDL runs as `migrator`, not `app_rw` — app_rw never does DDL), or a bug in `relay_ensure_partitions`.
4. Confirm the T+2 buffer requirement vs current date — how much runway remains before an insert would fail.

## Mitigation
**Immediate**
- Manually create the missing partitions now: run `relay_ensure_partitions('<parent>', 2)` (as the DDL role) for each short parent, restoring the T+2 buffer.
- Re-run the housekeeping pre-create task and confirm `relay_missing_partitions('<parent>', 2)` returns no rows for each parent.

**Follow-up**
- Fix the root cause (revive beat schedule, fix DDL permissions/locks, or the function bug).
- Add alerting on the housekeeping task's failure/absence, not just the missing-partition canary, so it is caught even earlier.

## Escalation
DBA/backend on-call. Urgency scales with runway: if <7 days before a missing month's boundary, treat as SEV-1 (imminent write failure).

## Related RFC / runbooks
- RFC-002 §5.3 (partition lifecycle, `relay_ensure_partitions`, `relay_missing_partitions`, T+2 pre-create; DDL as `migrator`).
- Master rule 4 (migrations, lock/statement timeouts). `celery-task-failure-rate.md`.
