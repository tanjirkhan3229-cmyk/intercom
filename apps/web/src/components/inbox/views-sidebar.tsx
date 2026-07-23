"use client";

import { Inbox, LogOut } from "lucide-react";
import { useAuth } from "@/lib/auth";
import { useTeams } from "@/lib/hooks";
import { STATIC_VIEWS, teamViewId } from "@/lib/views";
import { cn } from "@/lib/utils";

/**
 * Left pane: the views a P0.5 agent can pivot on — You, Unassigned, All open, Snoozed, Closed —
 * plus one entry per team inbox. Selecting a view is URL-driven (see the page) so a refresh
 * restores it (RFC P0.5 acceptance). Per-view live counts land with custom views in P1.7.
 */
export function ViewsSidebar({
  activeView,
  onSelect,
}: {
  activeView: string;
  onSelect: (viewId: string) => void;
}) {
  const { session, logout } = useAuth();
  const teams = useTeams();

  return (
    <nav className="flex h-full flex-col bg-muted/30">
      <div className="flex items-center gap-2 px-4 py-3 text-sm font-semibold">
        <Inbox className="h-4 w-4" />
        <span>{session?.workspace.name ?? "Relay"}</span>
      </div>

      <div className="flex-1 overflow-y-auto px-2 py-1">
        <ul className="space-y-0.5">
          {STATIC_VIEWS.map((v) => (
            <li key={v.id}>
              <ViewButton
                label={v.label}
                active={activeView === v.id}
                onClick={() => onSelect(v.id)}
              />
            </li>
          ))}
        </ul>

        {teams.data && teams.data.length > 0 && (
          <>
            <p className="px-3 pb-1 pt-4 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
              Teams
            </p>
            <ul className="space-y-0.5">
              {teams.data.map((t) => {
                const id = teamViewId(t.id);
                return (
                  <li key={t.id}>
                    <ViewButton label={t.name} active={activeView === id} onClick={() => onSelect(id)} />
                  </li>
                );
              })}
            </ul>
          </>
        )}
      </div>

      <div className="border-t border-border px-3 py-2">
        <div className="flex items-center justify-between gap-2">
          <div className="min-w-0">
            <p className="truncate text-xs font-medium">{session?.admin.name}</p>
            <p className="truncate text-[11px] text-muted-foreground">{session?.admin.email}</p>
          </div>
          <button
            onClick={() => void logout()}
            title="Sign out"
            className="rounded p-1.5 text-muted-foreground hover:bg-accent hover:text-accent-foreground"
          >
            <LogOut className="h-4 w-4" />
          </button>
        </div>
      </div>
    </nav>
  );
}

function ViewButton({
  label,
  active,
  onClick,
}: {
  label: string;
  active: boolean;
  onClick: () => void;
}) {
  return (
    <button
      onClick={onClick}
      aria-current={active ? "page" : undefined}
      className={cn(
        "flex w-full items-center rounded-md px-3 py-1.5 text-sm transition-colors",
        active
          ? "bg-accent font-medium text-accent-foreground"
          : "text-muted-foreground hover:bg-accent/60 hover:text-foreground",
      )}
    >
      {label}
    </button>
  );
}
