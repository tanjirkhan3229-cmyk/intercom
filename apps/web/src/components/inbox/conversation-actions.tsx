"use client";

import { Check, ChevronDown, Clock, Tag as TagIcon, UserPlus, X } from "lucide-react";
import * as React from "react";
import { useAuth } from "@/lib/auth";
import { useAssign, useSetState, useTags, useTagMutations, useTeams } from "@/lib/hooks";
import { Button } from "@/components/ui/button";
import { Input, Menu, MenuItem, Badge } from "@/components/ui/primitives";
import type { Conversation } from "@/lib/types";

/** Snooze presets → absolute ISO timestamps (business-hours-aware snoozing lands in P1.7). */
export function snoozePresets(now = new Date()): { label: string; until: string }[] {
  const hours = (h: number) => new Date(now.getTime() + h * 3600_000).toISOString();
  const tomorrow9 = new Date(now);
  tomorrow9.setDate(now.getDate() + 1);
  tomorrow9.setHours(9, 0, 0, 0);
  const nextWeek = new Date(now);
  nextWeek.setDate(now.getDate() + 7);
  nextWeek.setHours(9, 0, 0, 0);
  return [
    { label: "In 1 hour", until: hours(1) },
    { label: "In 3 hours", until: hours(3) },
    { label: "Tomorrow, 9am", until: tomorrow9.toISOString() },
    { label: "Next week", until: nextWeek.toISOString() },
  ];
}

export function AssignMenu({ conversation }: { conversation: Conversation }) {
  const { session } = useAuth();
  const teams = useTeams();
  const assign = useAssign(conversation.id);
  const assignedToMe = conversation.assignee_id === session?.admin.id;

  return (
    <Menu
      trigger={({ toggle }) => (
        <Button variant="outline" size="sm" onClick={toggle}>
          <UserPlus className="h-3.5 w-3.5" />
          {assignedToMe ? "You" : conversation.assignee_id ? "Assigned" : "Assign"}
          <ChevronDown className="h-3.5 w-3.5" />
        </Button>
      )}
    >
      {(close) => (
        <>
          <MenuItem
            onClick={() => {
              assign.mutate({ assigneeId: session?.admin.id });
              close();
            }}
          >
            <Check className={assignedToMe ? "h-3.5 w-3.5" : "h-3.5 w-3.5 opacity-0"} />
            Assign to me
          </MenuItem>
          {teams.data && teams.data.length > 0 && (
            <>
              <div className="my-1 border-t border-border" />
              <p className="px-2 py-1 text-[11px] font-semibold uppercase text-muted-foreground">
                Teams
              </p>
              {teams.data.map((t) => (
                <MenuItem
                  key={t.id}
                  onClick={() => {
                    assign.mutate({ teamId: t.id });
                    close();
                  }}
                >
                  <span className="h-3.5 w-3.5" />
                  {t.name}
                </MenuItem>
              ))}
            </>
          )}
        </>
      )}
    </Menu>
  );
}

export function SnoozeMenu({ conversation }: { conversation: Conversation }) {
  const setState = useSetState(conversation.id);
  return (
    <Menu
      align="end"
      trigger={({ toggle }) => (
        <Button variant="outline" size="sm" onClick={toggle}>
          <Clock className="h-3.5 w-3.5" />
          Snooze
        </Button>
      )}
    >
      {(close) => (
        <>
          {snoozePresets().map((p) => (
            <MenuItem
              key={p.label}
              onClick={() => {
                setState.mutate({ state: "snoozed", snoozedUntil: p.until });
                close();
              }}
            >
              <Clock className="h-3.5 w-3.5" />
              {p.label}
            </MenuItem>
          ))}
        </>
      )}
    </Menu>
  );
}

export function StateButton({ conversation }: { conversation: Conversation }) {
  const setState = useSetState(conversation.id);
  const closed = conversation.state === "closed";
  return (
    <Button
      size="sm"
      variant={closed ? "outline" : "default"}
      onClick={() => setState.mutate({ state: closed ? "open" : "closed" })}
      disabled={setState.isPending}
    >
      {closed ? "Reopen" : "Close"}
    </Button>
  );
}

export function TagEditor({ conversationId }: { conversationId: string }) {
  const tags = useTags(conversationId);
  const { add, remove } = useTagMutations(conversationId);
  const [value, setValue] = React.useState("");

  const submit = () => {
    const name = value.trim();
    if (!name) return;
    add.mutate(name);
    setValue("");
  };

  return (
    <div className="flex flex-wrap items-center gap-1.5">
      {(tags.data ?? []).map((t) => (
        <Badge key={t.name} variant="outline" className="gap-1">
          {t.name}
          <button
            onClick={() => remove.mutate(t.name)}
            className="text-muted-foreground hover:text-foreground"
            aria-label={`Remove tag ${t.name}`}
          >
            <X className="h-3 w-3" />
          </button>
        </Badge>
      ))}
      <Menu
        trigger={({ toggle }) => (
          <button
            onClick={toggle}
            className="inline-flex items-center gap-1 rounded-full border border-dashed border-input px-2 py-0.5 text-xs text-muted-foreground hover:text-foreground"
          >
            <TagIcon className="h-3 w-3" />
            Tag
          </button>
        )}
      >
        {(close) => (
          <div className="p-1">
            <Input
              autoFocus
              value={value}
              onChange={(e) => setValue(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") {
                  e.preventDefault();
                  submit();
                  close();
                }
              }}
              placeholder="Add tag…"
              className="h-8"
            />
          </div>
        )}
      </Menu>
    </div>
  );
}
