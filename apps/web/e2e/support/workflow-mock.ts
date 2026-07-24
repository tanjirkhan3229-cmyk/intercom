import type { Page, Route } from "@playwright/test";
import type {
  Workflow,
  WorkflowGraph,
  WorkflowRun,
  WorkflowRunStep,
  WorkflowSummary,
  WorkflowVersion,
  RunStatus,
  NodeType,
} from "../../src/lib/workflows/contract";
import { NON_TERMINAL_RUN_STATUSES } from "../../src/lib/workflows/contract";
import { validateGraph } from "../../src/lib/workflows/validate";
import { evaluate } from "../../src/lib/workflows/predicate";
import { botTargets, nodesById, triggerOf } from "../../src/lib/workflows/graph-utils";

/**
 * A stateful, contract-faithful in-memory workflow backend, served to the app via Playwright route
 * interception. It reuses the very validators/evaluators the UI ships (`validateGraph`, `evaluate`,
 * graph traversal) so a graph behaves identically here and against the real P1.5 engine. It also
 * mocks auth/teams/attribute-definitions, so workflow e2e is fully hermetic — no API stack needed.
 *
 * When the real P1.5 backend is ready, set `E2E_WORKFLOW_BACKEND=real` (see `fixtures.ts`): the mock
 * is not installed and the same specs run against the live API. Specs assert only on rendered UI, so
 * they are backend-agnostic.
 */

const BASE_EPOCH = Date.UTC(2026, 0, 1, 0, 0, 0);

interface StoredWorkflow {
  id: string;
  name: string;
  status: "inactive" | "active";
  active_version_id: string | null;
  draft_version_id: string | null;
  created_at: string;
  updated_at: string;
}

export interface MockOptions {
  enabled?: boolean;
}

export interface SimulateOptions {
  /** Run-context the predicates read (defaults to a resolvable, out-of-office-hours context). */
  context?: Record<string, unknown>;
  /** Auto-advance through wait/bot_step nodes so the run completes (default true). */
  autoAdvance?: boolean;
  /** Force a specific node's step to fail (for the run-log / re-run test). */
  failAtNodeId?: string;
}

export interface WorkflowMock {
  enabled: boolean;
  /** Create + walk a run against the workflow's active (published) version. Returns the run id. */
  simulate: (workflowId: string, opts?: SimulateOptions) => string;
  /** Create a run pinned to the active version, left in the given status (default "waiting"). */
  seedRun: (workflowId: string, status?: RunStatus) => string;
  runStatus: (runId: string) => RunStatus | undefined;
  workflowIdFromUrl: (url: string) => string | null;
}

export async function installWorkflowMock(
  page: Page,
  options: MockOptions = {},
): Promise<WorkflowMock> {
  const enabled = options.enabled ?? true;
  if (!enabled) {
    return {
      enabled: false,
      simulate: () => {
        throw new Error("simulate() is only available with the mock backend");
      },
      seedRun: () => {
        throw new Error("seedRun() is only available with the mock backend");
      },
      runStatus: () => undefined,
      workflowIdFromUrl: (url) => /\/workflows\/(wfl_[^/?#]+)/.exec(url)?.[1] ?? null,
    };
  }

  // --- Store -----------------------------------------------------------------
  let seq = 0;
  const clock = () => new Date(BASE_EPOCH + seq * 1000).toISOString();
  const nextId = (prefix: string) => `${prefix}_${++seq}`;

  const workflows = new Map<string, StoredWorkflow>();
  const versions = new Map<string, WorkflowVersion>();
  const runs = new Map<string, WorkflowRun>();
  const steps = new Map<string, WorkflowRunStep[]>(); // runId -> steps

  const versionsOf = (workflowId: string): WorkflowVersion[] =>
    [...versions.values()]
      .filter((v) => v.workflow_id === workflowId)
      .sort((a, b) => b.version - a.version);

  const activeRunsOnOldVersions = (wf: StoredWorkflow): number =>
    [...runs.values()].filter(
      (r) =>
        r.workflow_id === wf.id &&
        (NON_TERMINAL_RUN_STATUSES as readonly string[]).includes(r.status) &&
        r.workflow_version_id !== wf.active_version_id,
    ).length;

  const summary = (wf: StoredWorkflow): WorkflowSummary => {
    const active = wf.active_version_id ? versions.get(wf.active_version_id) : null;
    return {
      id: wf.id,
      name: wf.name,
      status: wf.status,
      active_version_id: wf.active_version_id,
      active_version: active ? active.version : null,
      active_runs_on_old_versions: activeRunsOnOldVersions(wf),
      created_at: wf.created_at,
      updated_at: wf.updated_at,
    };
  };

  const detail = (wf: StoredWorkflow): Workflow => ({
    ...summary(wf),
    draft: wf.draft_version_id ? (versions.get(wf.draft_version_id) ?? null) : null,
    active: wf.active_version_id ? (versions.get(wf.active_version_id) ?? null) : null,
  });

  const newDraftVersion = (workflowId: string, graph: WorkflowGraph): WorkflowVersion => {
    const trigger = triggerOf(graph);
    const v: WorkflowVersion = {
      id: nextId("wfv"),
      workflow_id: workflowId,
      version: 0,
      graph,
      trigger_key: trigger?.trigger ?? "conversation.created",
      status: "draft",
      created_at: clock(),
      created_by: null,
    };
    versions.set(v.id, v);
    return v;
  };

  const createWorkflow = (name: string): Workflow => {
    const id = nextId("wfl");
    const draft = newDraftVersion(id, { nodes: [] });
    const wf: StoredWorkflow = {
      id,
      name,
      status: "inactive",
      active_version_id: null,
      draft_version_id: draft.id,
      created_at: clock(),
      updated_at: clock(),
    };
    workflows.set(id, wf);
    return detail(wf);
  };

  const publish = (wf: StoredWorkflow, graph: WorkflowGraph): WorkflowVersion => {
    const nextNumber =
      versionsOf(wf.id).reduce((max, v) => Math.max(max, v.version), 0) + 1;
    const trigger = triggerOf(graph);
    const v: WorkflowVersion = {
      id: nextId("wfv"),
      workflow_id: wf.id,
      version: nextNumber,
      graph,
      trigger_key: trigger?.trigger ?? "conversation.created",
      status: "published",
      created_at: clock(),
      created_by: null,
    };
    versions.set(v.id, v);
    wf.active_version_id = v.id;
    wf.status = "active";
    // A fresh draft continues from the just-published graph so further edits don't mutate the live
    // version (matches the real model: editing a live workflow creates a new draft version).
    const draft = newDraftVersion(wf.id, graph);
    wf.draft_version_id = draft.id;
    wf.updated_at = clock();
    return v;
  };

  // --- Mini-executor (mirror of graph.py.outgoing + predicates.evaluate) -----
  const botNext = (node: Record<string, unknown>): string | null => {
    const params = (node.params ?? {}) as Record<string, unknown>;
    if (node.bot === "collect") return typeof params.next === "string" ? params.next : null;
    const targets = botTargets(params);
    return targets[0] ?? null;
  };

  const walk = (
    graph: WorkflowGraph,
    runId: string,
    opts: Required<SimulateOptions>,
  ): { steps: WorkflowRunStep[]; status: RunStatus; currentNodeId: string | null; error: string | null } => {
    const byId = nodesById(graph);
    const trigger = triggerOf(graph);
    const out: WorkflowRunStep[] = [];
    const visited = new Set<string>();
    let status: RunStatus = "running";
    let error: string | null = null;
    let current: string | null = trigger ? trigger.id : null;

    const push = (
      nodeId: string,
      nodeType: NodeType | null,
      stepStatus: WorkflowRunStep["status"],
      stepError: string | null = null,
      actionType: string | null = null,
    ) => {
      out.push({
        id: nextId("wfs"),
        run_id: runId,
        node_id: nodeId,
        node_type: nodeType,
        status: stepStatus,
        action_type: actionType,
        result: {},
        error: stepError,
        attempt: 0,
        created_at: clock(),
        updated_at: clock(),
      });
    };

    for (let i = 0; i <= byId.size + 1 && current !== null; i++) {
      if (visited.has(current)) break; // each node runs at most once (graph.py invariant)
      visited.add(current);
      const node = byId.get(current) as Record<string, unknown> | undefined;
      if (!node) {
        status = "failed";
        error = `run references unknown node "${current}"`;
        break;
      }
      const nodeType = node.type as NodeType;

      if (opts.failAtNodeId === current) {
        push(current, nodeType, "failed", "simulated failure");
        status = "failed";
        error = "simulated failure";
        break;
      }

      if (nodeType === "end") {
        push(current, "end", "done");
        status = "completed";
        break;
      }
      if (nodeType === "wait") {
        if (opts.autoAdvance) {
          push(current, "wait", "done");
          current = typeof node.next === "string" ? node.next : null;
          continue;
        }
        push(current, "wait", "started");
        status = "waiting";
        break;
      }
      if (nodeType === "bot_step") {
        if (opts.autoAdvance) {
          push(current, "bot_step", "done");
          current = botNext(node);
          continue;
        }
        push(current, "bot_step", "started");
        status = "awaiting_input";
        break;
      }
      if (nodeType === "condition") {
        const branch = evaluate(node.predicate, opts.context);
        push(current, "condition", "done", null, null);
        current = typeof node[branch ? "true" : "false"] === "string"
          ? (node[branch ? "true" : "false"] as string)
          : null;
        continue;
      }
      // trigger / action
      push(current, nodeType, "done", null, typeof node.action === "string" ? node.action : null);
      current = typeof node.next === "string" ? node.next : null;
    }

    if (current === null && status === "running") {
      status = "failed";
      error = "run reached an unwired output";
    }
    return { steps: out, status, currentNodeId: status === "completed" ? null : current, error };
  };

  const defaultContext: Record<string, unknown> = {
    conversation: { state: "open", ai_status: "in_progress", channel: "chat" },
    contact: { email: "visitor@example.com" },
    env: { within_office_hours: false },
    event: { name: "conversation.created" },
  };

  const makeRun = (
    wf: StoredWorkflow,
    versionId: string,
    status: RunStatus,
    currentNodeId: string | null,
    error: string | null,
  ): WorkflowRun => {
    const version = versions.get(versionId);
    const run: WorkflowRun = {
      id: nextId("wfr"),
      workflow_id: wf.id,
      workflow_version_id: versionId,
      version: version ? version.version : 0,
      status,
      trigger_topic: version?.trigger_key ?? "conversation.created",
      subject_kind: "conversation",
      subject_id: "cnv_mock",
      context: defaultContext,
      current_node_id: currentNodeId,
      error,
      created_at: clock(),
      updated_at: clock(),
      completed_at: status === "completed" ? clock() : null,
    };
    runs.set(run.id, run);
    return run;
  };

  const simulate = (workflowId: string, opts: SimulateOptions = {}): string => {
    const wf = workflows.get(workflowId);
    if (!wf || !wf.active_version_id) throw new Error("workflow has no published version to run");
    const version = versions.get(wf.active_version_id);
    if (!version) throw new Error("active version missing");
    const runId = nextId("wfr");
    const resolved: Required<SimulateOptions> = {
      context: opts.context ?? defaultContext,
      autoAdvance: opts.autoAdvance ?? true,
      failAtNodeId: opts.failAtNodeId ?? "",
    };
    const result = walk(version.graph, runId, resolved);
    const run: WorkflowRun = {
      id: runId,
      workflow_id: wf.id,
      workflow_version_id: version.id,
      version: version.version,
      status: result.status,
      trigger_topic: version.trigger_key,
      subject_kind: "conversation",
      subject_id: "cnv_mock",
      context: resolved.context,
      current_node_id: result.currentNodeId,
      error: result.error,
      created_at: clock(),
      updated_at: clock(),
      completed_at: result.status === "completed" ? clock() : null,
    };
    runs.set(runId, run);
    steps.set(runId, result.steps);
    return runId;
  };

  const seedRun = (workflowId: string, status: RunStatus = "waiting"): string => {
    const wf = workflows.get(workflowId);
    if (!wf || !wf.active_version_id) throw new Error("workflow has no published version to run");
    const run = makeRun(wf, wf.active_version_id, status, null, null);
    steps.set(run.id, []);
    return run.id;
  };

  const rerun = (runId: string, fromNodeId: string): WorkflowRun | null => {
    const run = runs.get(runId);
    if (!run) return null;
    const version = versions.get(run.workflow_version_id);
    if (!version) return null;
    // Re-walk from the beginning with no failure injection; keeps the exactly-once ledger honest
    // by rebuilding the step list. `fromNodeId` documents intent (the failed node).
    void fromNodeId;
    const result = walk(version.graph, runId, {
      context: run.context,
      autoAdvance: true,
      failAtNodeId: "",
    });
    run.status = result.status;
    run.current_node_id = result.currentNodeId;
    run.error = result.error;
    run.updated_at = clock();
    run.completed_at = result.status === "completed" ? clock() : null;
    steps.set(runId, result.steps);
    return run;
  };

  // --- HTTP surface ----------------------------------------------------------
  const json = (route: Route, status: number, body: unknown) =>
    route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });

  const errorBody = (code: string, message: string, details?: Record<string, unknown>) => ({
    error: { code, message, request_id: "mock", details },
  });

  await page.route("**/v0/**", async (route) => {
    const request = route.request();
    const method = request.method();
    const url = new URL(request.url());
    const path = url.pathname;
    const body = (): Record<string, unknown> => {
      try {
        return (request.postDataJSON() as Record<string, unknown>) ?? {};
      } catch {
        return {};
      }
    };

    // --- Auth (hermetic) ---
    // Refresh succeeds (stands in for the httpOnly refresh cookie) so the app stays authenticated
    // across full page navigations — otherwise every goto() would bounce to /login.
    if (path === "/v0/auth/logout") return route.fulfill({ status: 204, body: "" });
    if (
      path === "/v0/auth/login" ||
      path === "/v0/auth/refresh" ||
      path === "/v0/auth/me"
    ) {
      const session = {
        admin: { id: "adm_mock", email: "owner@example.com", name: "Owner" },
        workspace: { id: "wrk_mock", name: "Mock Workspace", slug: "mock" },
        role: "owner" as const,
      };
      if (path === "/v0/auth/me") return json(route, 200, session);
      return json(route, 200, {
        ...session,
        access_token: "mock-access-token",
        token_type: "bearer",
        expires_in: 3600,
      });
    }
    if (path === "/v0/teams") {
      return json(route, 200, [{ id: "team_x", name: "Team X", created_at: clock() }]);
    }
    if (path === "/v0/attribute-definitions") {
      const entity = url.searchParams.get("entity") ?? "contact";
      const defs =
        entity === "contact"
          ? [{ id: "attr_plan", entity: "contact", name: "plan", data_type: "string", label: "Plan" }]
          : [];
      return json(route, 200, defs);
    }

    // --- Workflows ---
    if (path === "/v0/workflows" && method === "GET") {
      return json(route, 200, {
        items: [...workflows.values()].map(summary),
        next_cursor: null,
      });
    }
    if (path === "/v0/workflows" && method === "POST") {
      const name = typeof body().name === "string" ? (body().name as string) : "Untitled";
      return json(route, 201, createWorkflow(name));
    }

    const rest = path.replace(/^\/v0\/workflows\/?/, "");
    const segs = rest.length > 0 ? rest.split("/") : [];

    // Run endpoints: /v0/workflows/runs/{runId}[/steps|/rerun]
    if (segs[0] === "runs") {
      const runId = segs[1] ?? "";
      const run = runs.get(runId);
      if (segs.length === 2 && method === "GET") {
        return run
          ? json(route, 200, run)
          : json(route, 404, errorBody("not_found", "run not found"));
      }
      if (segs[2] === "steps" && method === "GET") {
        return json(route, 200, steps.get(runId) ?? []);
      }
      if (segs[2] === "rerun" && method === "POST") {
        const updated = rerun(runId, String(body().from_node_id ?? ""));
        return updated
          ? json(route, 200, updated)
          : json(route, 404, errorBody("not_found", "run not found"));
      }
      return json(route, 404, errorBody("not_found", "unknown run route"));
    }

    // Workflow-scoped endpoints: /v0/workflows/{id}[/...]
    const id = segs[0] ?? "";
    const wf = workflows.get(id);
    if (!wf) return json(route, 404, errorBody("not_found", "workflow not found"));

    if (segs.length === 1) {
      if (method === "GET") return json(route, 200, detail(wf));
      if (method === "PATCH") {
        const b = body();
        if (typeof b.name === "string") wf.name = b.name;
        if (b.status === "active" || b.status === "inactive") wf.status = b.status;
        wf.updated_at = clock();
        return json(route, 200, detail(wf));
      }
      if (method === "DELETE") {
        workflows.delete(id);
        return route.fulfill({ status: 204, body: "" });
      }
    }
    if (segs[1] === "draft" && method === "PUT") {
      const graph = body().graph as WorkflowGraph;
      let draft = wf.draft_version_id ? versions.get(wf.draft_version_id) : undefined;
      if (!draft) {
        draft = newDraftVersion(wf.id, graph);
        wf.draft_version_id = draft.id;
      } else {
        draft.graph = graph;
        draft.trigger_key = triggerOf(graph)?.trigger ?? draft.trigger_key;
        draft.created_at = clock();
      }
      wf.updated_at = clock();
      return json(route, 200, draft);
    }
    if (segs[1] === "publish" && method === "POST") {
      const graph = body().graph as WorkflowGraph;
      const errors = validateGraph(graph).filter((e) => e.severity === "error");
      if (errors.length > 0) {
        const first = errors[0];
        return json(
          route,
          422,
          errorBody("validation_error", first?.message ?? "invalid graph", {
            path: first?.path,
            node_id: first?.nodeId,
          }),
        );
      }
      return json(route, 201, publish(wf, graph));
    }
    if (segs[1] === "versions" && method === "GET") {
      if (segs[2]) {
        const v = versions.get(segs[2]);
        return v ? json(route, 200, v) : json(route, 404, errorBody("not_found", "version"));
      }
      return json(route, 200, { items: versionsOf(wf.id), next_cursor: null });
    }
    if (segs[1] === "runs" && method === "GET") {
      const statusFilter = url.searchParams.get("status");
      const versionFilter = url.searchParams.get("version_id");
      const items = [...runs.values()]
        .filter((r) => r.workflow_id === wf.id)
        .filter((r) => !statusFilter || r.status === statusFilter)
        .filter((r) => !versionFilter || r.workflow_version_id === versionFilter)
        .sort((a, b) => (a.created_at < b.created_at ? 1 : -1));
      return json(route, 200, { items, next_cursor: null });
    }

    return json(route, 404, errorBody("not_found", "unknown workflow route"));
  });

  return {
    enabled: true,
    simulate,
    seedRun,
    runStatus: (runId) => runs.get(runId)?.status,
    workflowIdFromUrl: (u) => /\/workflows\/(wfl_[^/?#]+)/.exec(u)?.[1] ?? null,
  };
}
