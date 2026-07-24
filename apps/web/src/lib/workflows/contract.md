# Workflow REST contract (P1.6 UI ↔ P1.5 engine)

This is the REST envelope the **P1.6 workflow builder UI** codes against, authored so the **P1.5
backend** (owner of `apps/api/src/relay/modules/automation/`) can implement it directly. The graph
shape, predicate AST, version/run/step model, and public-id prefixes are already fixed by the
backend and are **transcribed** in `contract.ts` — this doc only defines the HTTP surface, which is
not yet written (`automation/router.py` + `service.py` are stubs at time of writing).

TypeScript types for every shape below live in `./contract.ts`.

## Fixed backend facts we depend on (do not change without telling the UI team)

- **Graph**: `automation/graph.py` — `{ nodes: Node[] }`, exactly one `trigger` node; edges are node
  fields (`next`, `true`/`false`, bot `params.options[].next`/`default_next`/`collect.next`);
  `validate_graph` raises `ValidationError(message, details={"path": ...})` on the first problem.
- **Predicates**: `core/predicates.py` — grammar as transcribed.
- **Model**: `automation/models.py` — `workflows(status inactive|active, active_version_id)`,
  `workflow_versions(version:int, graph, trigger_key, status draft|published|archived)`,
  `workflow_runs(workflow_version_id ← the pin, status, context, current_node_id, …)`,
  `workflow_run_steps(node_id, status started|done|failed|skipped, action_type, attempt)
  UNIQUE(run_id, node_id)`.
- **Public ids**: `core/ids.py` — `wfl_` workflow, `wfv_` version, `wfr_` run.

## ⚠️ One requirement on the backend

The UI stores canvas layout as a **`ui: {x, y}` key on each node** inside the graph JSON.
`graph.py` ignores unknown node keys, so this passes validation. **The service must persist the
`graph` JSONB verbatim (round-trip unknown keys).** If for any reason unknown keys are stripped,
tell the UI team — the documented fallback is a sidecar `layout: Record<nodeId,{x,y}>` column on
`workflow_versions`, and the UI will switch to it.

## Conventions

- Base prefix `/v0`. All mutating endpoints accept an `Idempotency-Key` header.
- Lists use the keyset `Page<T>` envelope (`{ items, next_cursor }`) with a `cursor` query param.
- Errors use the standard envelope `{ error: { code, message, request_id?, details? } }`.
- UUIDs serialize as prefixed public ids; datetimes as ISO-8601.

## Endpoints

| Method | Path | Body | Returns | Notes |
|---|---|---|---|---|
| GET | `/v0/workflows` | — | `Page<WorkflowSummary>` | `?cursor&limit&status` |
| POST | `/v0/workflows` | `{name}` | `Workflow` (201) | creates the head **and** an empty draft version (`{nodes:[]}`) |
| GET | `/v0/workflows/{id}` | — | `Workflow` | embeds `draft` + `active` versions |
| PATCH | `/v0/workflows/{id}` | `{name?, status?}` | `Workflow` | `status` toggles `inactive`/`active` (enable/disable) |
| DELETE | `/v0/workflows/{id}` | — | 204 | |
| PUT | `/v0/workflows/{id}/draft` | `{graph}` | `WorkflowVersion` | idempotent replace of the draft version's graph; **does not validate** (drafts may be incomplete) |
| POST | `/v0/workflows/{id}/publish` | `{graph}` | `WorkflowVersion` (201) | validates via `graph.py`; on success promotes to a new immutable published version (`version`++), sets `workflows.active_version_id`; **422** on invalid graph |
| GET | `/v0/workflows/{id}/versions` | — | `Page<WorkflowVersion>` | newest first |
| GET | `/v0/workflows/{id}/versions/{vid}` | — | `WorkflowVersion` | immutable |
| GET | `/v0/workflows/{id}/runs` | — | `Page<WorkflowRun>` | `?status&version_id&cursor` |
| GET | `/v0/workflows/runs/{runId}` | — | `WorkflowRun` | `version` is resolved from `workflow_version_id` |
| GET | `/v0/workflows/runs/{runId}/steps` | — | `WorkflowRunStep[]` | ordered by `created_at`; `node_type` resolved from the pinned graph |
| POST | `/v0/workflows/runs/{runId}/rerun` | `{from_node_id}` | `WorkflowRun` | re-enqueues from a failed node; idempotent-effect steps only |

### Publish validation (422)

`publish` runs `graph.validate_graph(body.graph)`. On failure return the standard error envelope
with `code: "validation_error"`, the raised `message`, and `details.path` pointing at the offending
node/field (exactly what `graph.py` already raises). The UI maps `details.path` back onto the node.
The UI **also** validates client-side (mirror in `validate.ts`) and blocks publish before the call —
the server is the authoritative gate, the client is the fast/rich surface.

### "Runs on old versions" indicator

`WorkflowSummary.active_runs_on_old_versions` = count of runs whose `status` is non-terminal
(`running|waiting|suspended|awaiting_input`) and whose `workflow_version_id != active_version_id`.
This drives the badge that proves in-flight runs stay pinned to their version across a publish.

## Node config the UI emits (subset used by the acceptance flow)

- Trigger `conversation.created`: `{type:"trigger", trigger:"conversation.created", filter?, next}`.
- Condition (incl. "outside office hours" preset →
  `{op:"eq", field:"env.within_office_hours", value:false}`): `{type:"condition", predicate, true, false}`.
- Bot `collect` (collect email → contact attribute):
  `{type:"bot_step", bot:"collect", params:{prompt, target:"contact", key:"email", next}}`.
- Action `hand_to_aide`: `{type:"action", action:"hand_to_aide", params:{}, next}`.
- Action `route_to_team`: `{type:"action", action:"route_to_team", params:{team_id}, next}`.
- `end`: `{type:"end"}`.

## ⚠️ Reconciliation needed — the built backend has diverged from this contract

As of P1.5's current `router.py`/`schemas.py`/`service.py`, the backend REST surface differs from the
contract above. The UI codes against this contract via the mock; **flipping `E2E_WORKFLOW_BACKEND=real`
will not work until these are reconciled.** `apps/web/src/lib/api.ts` is the single point that needs
to change on the UI side. The deltas (backend → what the UI expects):

1. **Run routes are `/v0/workflow_runs/...`, not `/v0/workflows/runs/...`.** Align one way; the UI
   currently calls `/v0/workflows/runs/{id}`.
2. **Publish takes `{version_id}` and returns `WorkflowOut`**; the UI/mock send `{graph}` and expect
   the created `WorkflowVersion`. The backend flow is two-step: `POST /workflows/{id}/versions {graph}`
   (validates → 422 with `details.path`) then `POST /workflows/{id}/publish {version_id}`. The UI's
   one-shot `publish {graph}` should either be split, or the backend should accept `{graph}`.
3. **No draft endpoint.** The UI autosaves via `PUT /workflows/{id}/draft {graph}`; the backend has
   no draft concept — it creates a (validated) version per save. **Blocker:** validated-only versions
   can't hold an incomplete draft. Decide: add a `draft` slot (unvalidated) or the UI stops autosaving
   incomplete graphs.
4. **`WorkflowVersionOut` has no `graph` field and there is no `GET /versions/{id}`.** **Blocker for the
   builder:** it cannot reload a saved graph to render the canvas. The version DTO must return `graph`,
   or add a `GET` that does.
5. **`WorkflowOut` lacks `active_version` (number), `active_runs_on_old_versions`, and embedded
   `draft`/`active`.** The list "runs on old version" badge and the builder's draft load depend on
   these (or on #3/#4). Add them, or the UI derives from separate calls.
6. **`WorkflowRunOut` lacks `version` (number) and `context`; `WorkflowRunStepOut` lacks `run_id` and
   `node_type`.** The run list shows `vN` and the timeline labels steps by type — add these (they are
   cheap to resolve server-side from the pinned graph) or the UI resolves them client-side.
7. **Re-run:** the backend exposes `POST /workflow_runs/{id}/cancel` and `POST /workflow_runs/{id}/input`
   (bot resume), **not** `/rerun`. The run-log "re-run from failed step" needs a `/rerun {from_node_id}`
   endpoint, or the UI drops that affordance until it exists.

The graph shape + predicate AST + validation rules (cycles rejected, `call_webhook` POST-only with a
string→string `headers` map) are already reconciled — the UI validator mirrors `graph.py` exactly.

## Reused existing endpoints

- `GET /v0/teams` → `Team[]` (route-to-team picker) — already implemented.
- `GET /v0/attribute-definitions?entity=contact|company` → `AttributeDefinition[]` (predicate field
  picker) — already implemented (CRM, P0.2).
