# Runbook — RLS audit failure (tenant table missing forced RLS)

**Severity:** SEV-1 — **SECURITY CRITICAL.** Potential cross-tenant data exposure.

## Alert name
`RLSAuditFailure`

## Metric / expression
Not a Prometheus series — this is the `scripts/audit_rls.py` gate (run in CI on every migration/PR and, recommended, as a post-deploy canary). It exits non-zero and prints `rls-audit: FAIL` with offending tables when any table in `public` carrying a `workspace_id` column is **not** RLS ENABLED + FORCED + backed by ≥1 policy (the repo installs `ws_isolation`).

```
rls-audit: FAIL
  - <table>: RLS not ENABLED (relrowsecurity=false)
  - <table>: RLS not FORCED (relforcerowsecurity=false)
  - <table>: no policy in pg_policies
```

## Symptom / user impact
Tenancy is sacred (master rule 1). A tenant table without forced RLS means a query without the `app.ws` GUC set could return **another workspace's rows** — a cross-tenant leak, which is a hard Phase-0 exit blocker (RFC-000 §5: zero cross-tenant leakage). This is treated as a security incident until proven otherwise.

## Dashboards to open
- CI job output for `scripts/audit_rls.py`.
- The offending migration diff (which migration added the table without `create_tenant_table`).
- The cross-tenant test suite result: `apps/api/tests/integration/test_tenancy_rls.py`.

## Diagnosis steps
1. Read the offender lines. For each table, which check failed (ENABLED / FORCED / policy)?
2. Find the migration that created the table. Was it created via `relay.core.rls.create_tenant_table` (which enables + forces RLS + installs the policy automatically)? If created with raw DDL, that is the bug.
3. Was the table actually accessed by tenant traffic between the misconfiguration landing and detection? Check logs for queries against that table without `app.ws`.
4. Run the cross-tenant suite locally against the affected schema — it asserts an unset `app.ws` returns **zero** rows from tenant tables; a failure there is a live leak.

## Mitigation
**Immediate**
- **Block the deploy / roll back** the migration that introduced the unprotected table (CI should already have failed — if this fired post-deploy, roll back now).
- Apply the missing controls: `ALTER TABLE <t> ENABLE ROW LEVEL SECURITY; ALTER TABLE <t> FORCE ROW LEVEL SECURITY;` and install the `ws_isolation` policy (or re-run the table through `create_tenant_table`).
- If any cross-tenant read may have occurred, open a security incident and assess exposure per the SOC 2 process.

**Follow-up**
- Add/verify the mandatory cross-tenant leakage test for the new table (master rule 1 — every tenant table ships with one).
- Confirm CI runs `audit_rls.py` on the gate so this cannot merge again.

## Escalation
**Page security + backend on-call immediately.** Do not close until `audit_rls.py` is green AND `test_tenancy_rls.py` passes for the affected table.

## Related RFC / runbooks
- RFC-002 §7 (RLS enabled + forced), RFC-001 §10 (RBAC choke-point, PII). RFC-000 §5 (exit: zero leakage).
- Master rule 1 (tenancy). Evidence: `scripts/audit_rls.py`, `apps/api/tests/integration/test_tenancy_rls.py`.
- See `docs/phase0-exit-criteria.md` (zero cross-tenant leakage row).
