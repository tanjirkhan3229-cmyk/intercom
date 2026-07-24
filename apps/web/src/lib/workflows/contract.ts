/**
 * Workflow builder ↔ engine API contract (P1.6, RFC-001 §6.7 / RFC-002 §5.6).
 *
 * This file is the **single source of truth** for the workflow types the agent app consumes, and it
 * is a faithful transcription of the P1.5 backend (built in parallel on another track):
 *
 *  - Graph shape + vocabulary  ← `apps/api/src/relay/modules/automation/graph.py`
 *  - Predicate AST             ← `apps/api/src/relay/core/predicates.py`
 *  - Version/run/step model    ← `apps/api/src/relay/modules/automation/models.py`
 *  - Public-id prefixes         ← `apps/api/src/relay/core/ids.py` (wfl_ / wfv_ / wfr_)
 *
 * The REST envelope (DTOs + endpoints) is defined here and documented for the backend team in
 * `contract.md`. Keep this in lockstep with those Python modules — any divergence is a bug.
 *
 * ONE addition over `graph.py`'s node shape: every node may carry a UI-only `ui: {x, y}` layout
 * hint. `graph.py` ignores unknown node keys (it reads specific fields), so this round-trips through
 * validation untouched — provided the service persists the graph JSONB verbatim (see contract.md).
 */

// --- Predicate AST (mirror of core/predicates.py) -----------------------------

export type PredicateScalar = string | number | boolean | null;

export type LogicalOp = "and" | "or" | "not";
export type ValueOp = "eq" | "ne" | "gt" | "gte" | "lt" | "lte";
export type MembershipOp = "in" | "contains";
export type PresenceOp = "exists" | "not_exists";
export type ComparisonOp = ValueOp | MembershipOp | PresenceOp;
export type PredicateOp = LogicalOp | ComparisonOp;

export const LOGICAL_OPS: readonly LogicalOp[] = ["and", "or", "not"] as const;
export const VALUE_OPS: readonly ValueOp[] = ["eq", "ne", "gt", "gte", "lt", "lte"] as const;
export const MEMBERSHIP_OPS: readonly MembershipOp[] = ["in", "contains"] as const;
export const PRESENCE_OPS: readonly PresenceOp[] = ["exists", "not_exists"] as const;
export const COMPARISON_OPS: readonly ComparisonOp[] = [
  ...VALUE_OPS,
  ...MEMBERSHIP_OPS,
  ...PRESENCE_OPS,
];
export const ALL_PREDICATE_OPS: readonly PredicateOp[] = [...LOGICAL_OPS, ...COMPARISON_OPS];

/** Depth guard mirrors `_MAX_DEPTH` in predicates.py. */
export const PREDICATE_MAX_DEPTH = 32;

export type Predicate =
  | { op: "and" | "or"; clauses: Predicate[] }
  | { op: "not"; clause: Predicate }
  | { op: ValueOp; field: string; value: PredicateScalar }
  | { op: "in"; field: string; value: PredicateScalar[] }
  | { op: "contains"; field: string; value: PredicateScalar }
  | { op: PresenceOp; field: string };

// --- Graph vocabulary (mirror of automation/graph.py) -------------------------

export type NodeType = "trigger" | "condition" | "action" | "bot_step" | "wait" | "end";
export const NODE_TYPES: readonly NodeType[] = [
  "trigger",
  "condition",
  "action",
  "bot_step",
  "wait",
  "end",
] as const;

/** Trigger keys with a real outbox source wired in P1.5 (events.OUTBOX_TO_TRIGGER). */
export type SourcedTriggerKey =
  | "conversation.created"
  | "contact.message.created"
  | "contact.created"
  | "contact.updated"
  | "conversation.state_changed";
export const SOURCED_TRIGGER_KEYS: readonly SourcedTriggerKey[] = [
  "conversation.created",
  "contact.message.created",
  "contact.created",
  "contact.updated",
  "conversation.state_changed",
] as const;

/** Framework-ready triggers: authorable now, but inert until their source lands. */
export type FrameworkTriggerKey = "schedule" | "webhook_in" | "admin.action" | "attribute.changed";
export const FRAMEWORK_TRIGGER_KEYS: readonly FrameworkTriggerKey[] = [
  "schedule",
  "webhook_in",
  "admin.action",
  "attribute.changed",
] as const;

export type TriggerKey = SourcedTriggerKey | FrameworkTriggerKey;
export const TRIGGER_KEYS: readonly TriggerKey[] = [
  ...SOURCED_TRIGGER_KEYS,
  ...FRAMEWORK_TRIGGER_KEYS,
];

export type ActionType =
  | "assign"
  | "route_to_team"
  | "add_tag"
  | "set_attribute"
  | "snooze"
  | "close"
  | "send_reply"
  | "hand_to_aide"
  | "call_webhook"
  | "apply_sla";
export const ACTION_TYPES: readonly ActionType[] = [
  "assign",
  "route_to_team",
  "add_tag",
  "set_attribute",
  "snooze",
  "close",
  "send_reply",
  "hand_to_aide",
  "call_webhook",
  "apply_sla",
] as const;

export type BotKind = "ask_buttons" | "collect" | "disambiguate";
export const BOT_KINDS: readonly BotKind[] = ["ask_buttons", "collect", "disambiguate"] as const;

export type AttrTarget = "conversation" | "contact";
export const ATTR_TARGETS: readonly AttrTarget[] = ["conversation", "contact"] as const;

export type HttpMethod = "GET" | "POST" | "PUT" | "PATCH" | "DELETE";
export const HTTP_METHODS: readonly HttpMethod[] = ["GET", "POST", "PUT", "PATCH", "DELETE"] as const;

/** Ceiling mirrors `_MAX_NODES` in graph.py. */
export const MAX_NODES = 200;

// --- Node param shapes --------------------------------------------------------

export interface NodeUI {
  x: number;
  y: number;
}

export interface AssignParams {
  assignee_id?: string;
  team_id?: string;
}
export interface RouteToTeamParams {
  team_id: string;
}
export interface AddTagParams {
  name: string;
}
export interface SetAttributeParams {
  target: AttrTarget;
  key: string;
  value: PredicateScalar;
}
/** wait/snooze share a duration shape: exactly one of `seconds` (>0) or ISO `until`. */
export type DurationParams = { seconds: number } | { until: string };
export type SnoozeParams = DurationParams;
export type WaitParams = DurationParams;
export interface SendReplyParams {
  body: string;
}
export interface CallWebhookParams {
  url: string;
  method?: HttpMethod;
  headers?: Record<string, string>;
  body?: unknown;
}
export type CloseParams = Record<string, never>;
export type HandToAideParams = Record<string, never>;
export type ApplySlaParams = Record<string, unknown>;

export interface BotOption {
  label: string;
  value: string;
  next: string;
  /** Stable UI-only handle key (like node `ui`, persisted verbatim). Decouples canvas wiring from
   * the editable `value`, so renaming an option's value never severs its connection. Optional so
   * externally-authored graphs (keyed by `value`) still render. */
  id?: string;
}
export interface AskButtonsParams {
  prompt: string;
  options: BotOption[];
  default_next?: string;
}
export type DisambiguateParams = AskButtonsParams;
export interface CollectParams {
  prompt: string;
  target: AttrTarget;
  key: string;
  next: string;
}

// --- Node shapes (mirror of graph.py `_validate_node`) ------------------------

interface BaseNode {
  id: string;
  ui?: NodeUI;
}

export interface TriggerNode extends BaseNode {
  type: "trigger";
  trigger: TriggerKey;
  filter?: Predicate | null;
  next: string;
}

export interface ConditionNode extends BaseNode {
  type: "condition";
  predicate: Predicate;
  /** target when the predicate is true */
  true: string;
  /** target when the predicate is false */
  false: string;
}

type ActionNodeOf<A extends ActionType, P> = BaseNode & {
  type: "action";
  action: A;
  params: P;
  next: string;
};
export type ActionNode =
  | ActionNodeOf<"assign", AssignParams>
  | ActionNodeOf<"route_to_team", RouteToTeamParams>
  | ActionNodeOf<"add_tag", AddTagParams>
  | ActionNodeOf<"set_attribute", SetAttributeParams>
  | ActionNodeOf<"snooze", SnoozeParams>
  | ActionNodeOf<"close", CloseParams>
  | ActionNodeOf<"send_reply", SendReplyParams>
  | ActionNodeOf<"hand_to_aide", HandToAideParams>
  | ActionNodeOf<"call_webhook", CallWebhookParams>
  | ActionNodeOf<"apply_sla", ApplySlaParams>;

type BotStepNodeOf<K extends BotKind, P> = BaseNode & {
  type: "bot_step";
  bot: K;
  params: P;
};
export type BotStepNode =
  | BotStepNodeOf<"ask_buttons", AskButtonsParams>
  | BotStepNodeOf<"disambiguate", DisambiguateParams>
  | BotStepNodeOf<"collect", CollectParams>;

export interface WaitNode extends BaseNode {
  type: "wait";
  params: WaitParams;
  next: string;
}

export interface EndNode extends BaseNode {
  type: "end";
}

export type WorkflowNode =
  | TriggerNode
  | ConditionNode
  | ActionNode
  | BotStepNode
  | WaitNode
  | EndNode;

export interface WorkflowGraph {
  nodes: WorkflowNode[];
}

// --- Graph validation errors (client-side; server returns one at a time) ------

export type GraphErrorSeverity = "error" | "warning";
export interface GraphError {
  /** The offending node id, when the error is localized to a node. */
  nodeId?: string;
  /** Dotted path echoing the backend's `details.path` (e.g. "node[n1].params"). */
  path?: string;
  /** Stable machine code (client codes; the server sends its own via the error envelope). */
  code: string;
  message: string;
  severity: GraphErrorSeverity;
}

// --- REST DTOs (mirror of models.py, API-serialized) --------------------------
//
// Convention (RFC-002 §5.1): UUIDs are exposed as prefixed base62 public ids
// (wfl_/wfv_/wfr_); datetimes are ISO-8601 strings.

export type WorkflowStatus = "inactive" | "active";
export const WORKFLOW_STATUSES: readonly WorkflowStatus[] = ["inactive", "active"] as const;

export type VersionStatus = "draft" | "published" | "archived";
export const VERSION_STATUSES: readonly VersionStatus[] = [
  "draft",
  "published",
  "archived",
] as const;

export type RunStatus =
  | "running"
  | "waiting"
  | "suspended"
  | "awaiting_input"
  | "completed"
  | "failed"
  | "cancelled";
export const RUN_STATUSES: readonly RunStatus[] = [
  "running",
  "waiting",
  "suspended",
  "awaiting_input",
  "completed",
  "failed",
  "cancelled",
] as const;
/** Runs that are still in flight (used for the "runs on old versions" indicator). */
export const NON_TERMINAL_RUN_STATUSES: readonly RunStatus[] = [
  "running",
  "waiting",
  "suspended",
  "awaiting_input",
] as const;

export type StepStatus = "started" | "done" | "failed" | "skipped";
export const STEP_STATUSES: readonly StepStatus[] = [
  "started",
  "done",
  "failed",
  "skipped",
] as const;

export interface WorkflowVersion {
  id: string;
  workflow_id: string;
  version: number;
  graph: WorkflowGraph;
  trigger_key: TriggerKey;
  status: VersionStatus;
  created_at: string;
  created_by: string | null;
}

export interface WorkflowSummary {
  id: string;
  name: string;
  status: WorkflowStatus;
  /** The published version new runs execute while `status === "active"`. */
  active_version_id: string | null;
  active_version: number | null;
  /** Count of non-terminal runs pinned to a version other than `active_version_id`. */
  active_runs_on_old_versions: number;
  created_at: string;
  updated_at: string;
}

export interface Workflow extends WorkflowSummary {
  /** The current editable draft version (status "draft"), or null if none exists yet. */
  draft: WorkflowVersion | null;
  /** The published/active version summary, or null before first publish. */
  active: WorkflowVersion | null;
}

export interface WorkflowRun {
  id: string;
  workflow_id: string;
  workflow_version_id: string;
  /** Version number resolved from `workflow_version_id` (denormalized for display). */
  version: number;
  status: RunStatus;
  trigger_topic: string;
  subject_kind: string | null;
  subject_id: string | null;
  context: Record<string, unknown>;
  current_node_id: string | null;
  error: string | null;
  created_at: string;
  updated_at: string;
  completed_at: string | null;
}

export interface WorkflowRunStep {
  id: string;
  run_id: string;
  node_id: string;
  /** Node type resolved from the pinned graph (for the timeline UI). */
  node_type: NodeType | null;
  status: StepStatus;
  action_type: string | null;
  result: Record<string, unknown>;
  error: string | null;
  attempt: number;
  created_at: string;
  updated_at: string;
}

// --- CRM attribute definitions (existing endpoint, reused by field picker) ----

export type AttributeDataType = "string" | "number" | "boolean" | "date" | "list";
export interface AttributeDefinition {
  id: string;
  entity: "contact" | "company";
  name: string;
  data_type: AttributeDataType;
  label: string | null;
}

// --- Request bodies -----------------------------------------------------------

export interface WorkflowCreateInput {
  name: string;
}
export interface WorkflowPatchInput {
  name?: string;
  status?: WorkflowStatus;
}
export interface DraftInput {
  graph: WorkflowGraph;
}
export interface PublishInput {
  graph: WorkflowGraph;
}
export interface RerunInput {
  from_node_id: string;
}

// --- Run-context field catalog (for the predicate field picker) ---------------
//
// Advisory list of fields the executor places in a run's `context` (models.py:
// trigger payload + collected bot answers + action results). The picker also
// accepts free-text dotted paths, so this is a convenience, not a constraint.
// `env.within_office_hours` is how the "outside office hours" condition is authored
// (P1.7 populates it; the node is authorable today).

export interface ContextField {
  path: string;
  label: string;
  data_type: AttributeDataType;
}

export const CONTEXT_FIELD_CATALOG: readonly ContextField[] = [
  { path: "conversation.state", label: "Conversation · state", data_type: "string" },
  { path: "conversation.channel", label: "Conversation · channel", data_type: "string" },
  { path: "conversation.priority", label: "Conversation · priority", data_type: "boolean" },
  { path: "conversation.assignee_id", label: "Conversation · assignee", data_type: "string" },
  { path: "conversation.team_id", label: "Conversation · team", data_type: "string" },
  { path: "conversation.ai_status", label: "Conversation · Aide status", data_type: "string" },
  { path: "contact.email", label: "Contact · email", data_type: "string" },
  { path: "contact.name", label: "Contact · name", data_type: "string" },
  { path: "contact.phone", label: "Contact · phone", data_type: "string" },
  { path: "contact.external_id", label: "Contact · external id", data_type: "string" },
  {
    path: "env.within_office_hours",
    label: "Environment · within office hours",
    data_type: "boolean",
  },
  { path: "event.name", label: "Event · name", data_type: "string" },
] as const;

/** The predicate authored by the "outside office hours" condition preset. */
export const OUTSIDE_OFFICE_HOURS_PREDICATE: Predicate = {
  op: "eq",
  field: "env.within_office_hours",
  value: false,
};
