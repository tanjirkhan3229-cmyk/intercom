/**
 * Inbox views (RFC-000 §2.2). Phase-0 set: You, Unassigned, Team inboxes, All open, Snoozed,
 * Closed. A view id is URL-serialisable (survives refresh, RFC acceptance) and compiles to the
 * `GET /conversations` filter params. Team views are dynamic (`team:{team_id}`).
 */

export type ListParams = {
  state: "open" | "snoozed" | "closed";
  assigneeId?: string;
  teamId?: string;
  unassigned?: boolean;
};

export const STATIC_VIEWS = [
  { id: "you", label: "You" },
  { id: "unassigned", label: "Unassigned" },
  { id: "all-open", label: "All open" },
  { id: "snoozed", label: "Snoozed" },
  { id: "closed", label: "Closed" },
] as const;

export const DEFAULT_VIEW = "you";

export function isTeamView(viewId: string): boolean {
  return viewId.startsWith("team:");
}

export function teamViewId(teamId: string): string {
  return `team:${teamId}`;
}

/** Compile a view id (+ the current admin) into `GET /conversations` params. */
export function paramsForView(viewId: string, adminId: string | undefined): ListParams {
  if (viewId.startsWith("team:")) {
    return { state: "open", teamId: viewId.slice("team:".length) };
  }
  switch (viewId) {
    case "unassigned":
      return { state: "open", unassigned: true };
    case "all-open":
      return { state: "open" };
    case "snoozed":
      return { state: "snoozed" };
    case "closed":
      return { state: "closed" };
    case "you":
    default:
      return { state: "open", assigneeId: adminId };
  }
}

/** The channel bucket a view listens on for realtime list updates (RFC-001 §6.3). */
export function inboxBucketForView(viewId: string): string {
  if (viewId.startsWith("team:")) return viewId.slice("team:".length);
  if (viewId === "unassigned") return "none";
  return "all";
}
