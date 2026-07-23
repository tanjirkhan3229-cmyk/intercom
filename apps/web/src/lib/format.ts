/** Small presentation helpers — pure, so they're trivially unit-testable and SSR-safe. */

/** Compact relative time, e.g. "just now", "4m", "3h", "2d", else a short date. */
export function timeAgo(iso: string | null | undefined, now: number = Date.now()): string {
  if (!iso) return "";
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "";
  const secs = Math.max(0, Math.round((now - then) / 1000));
  if (secs < 45) return "just now";
  const mins = Math.round(secs / 60);
  if (mins < 60) return `${mins}m`;
  const hrs = Math.round(mins / 60);
  if (hrs < 24) return `${hrs}h`;
  const days = Math.round(hrs / 24);
  if (days < 7) return `${days}d`;
  return new Date(iso).toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

/** How long a conversation has been waiting, from `waiting_since` (empty when not waiting). */
export function waitingFor(waitingSince: string | null, now: number = Date.now()): string {
  if (!waitingSince) return "";
  return timeAgo(waitingSince, now);
}

/** Best available display name for a contact. */
export function contactLabel(c: {
  name?: string | null;
  email?: string | null;
  external_id?: string | null;
  id: string;
}): string {
  return c.name || c.email || c.external_id || c.id;
}

/** Two-letter initials for an avatar chip. */
export function initials(label: string): string {
  const parts = label.trim().split(/\s+/).filter(Boolean);
  if (parts.length === 0) return "?";
  if (parts.length === 1) return parts[0]!.slice(0, 2).toUpperCase();
  return (parts[0]![0]! + parts[parts.length - 1]![0]!).toUpperCase();
}
