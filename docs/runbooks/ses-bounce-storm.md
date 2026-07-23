# Runbook — SES bounce / complaint storm

**Severity:** SEV-2 (sender reputation at risk; email channel degraded).

## Alert name
`SESBounceStorm`

## Metric / expression
Bounce/complaint handling runs as Celery tasks off inbound SES notifications. Alert on a spike in bounce/complaint processing plus SES's own bounce/complaint rate:

```promql
sum(rate(relay_celery_tasks_total{task=~".*bounce.*|.*complaint.*"}[5m])) > <baseline>
```

Primary signal is the SES account bounce rate (CloudWatch `Reputation.BounceRate`) crossing the 5% warning / 10% enforcement thresholds. Corroborate with outbound send task volume for the offending workspace.

## Symptom / user impact
A surge of bounces/complaints (bad list, spam-trap hits, a compromised/abusive tenant). Risk: AWS throttles or pauses the SES account, taking down email for **all** tenants. Campaigns and outbound replies delayed (RFC-001 §9 SES row).

## Dashboards to open
- **SES reputation (CloudWatch)** — BounceRate, ComplaintRate, sending quota/throttle state.
- **Worker / traffic** — bounce/complaint task rate, send task rate by workspace.
- **Suppression list** — growth rate, per-workspace concentration.

## Diagnosis steps
1. Is one workspace responsible? Correlate bounce spike with a specific tenant's campaign/send burst (W5).
2. Are token buckets already capping sends (RFC-001 §9)? They should be limiting steady rate.
3. Are bounced/complained addresses being added to the suppression list so repeat sends stop?
4. Hard bounces (bad addresses) vs soft (transient) vs complaints (spam reports) — complaints are the most reputation-damaging.

## Mitigation
**Immediate**
- Halt repeat offenders: the **suppression list** stops re-sends to bounced/complained addresses.
- Apply the **per-tenant send pause switch** to the offending workspace (RFC-001 §9) — do not let one tenant burn the shared SES reputation.
- If account-level throttling is imminent, pause the offending campaign entirely.

**Follow-up**
- Enforce list-hygiene / double opt-in for the tenant; review how the bad list entered.
- Confirm token-bucket caps and per-tenant send limits are correctly sized.
- Request SES quota review if reputation was dinged.

## Escalation
On-call + deliverability owner immediately if account-level BounceRate >10% or SES signals impending suspension (shared blast radius across all tenants).

## Related RFC / runbooks
- RFC-001 §9 (SES bounce storm row: token buckets, suppression list, per-tenant pause).
- `celery-task-failure-rate.md`.
