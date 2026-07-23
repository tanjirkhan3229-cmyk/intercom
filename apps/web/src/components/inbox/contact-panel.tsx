"use client";

import { useContact, useContactConversations, useContactEvents } from "@/lib/hooks";
import { contactLabel, initials, timeAgo } from "@/lib/format";
import { Avatar, Badge } from "@/components/ui/primitives";
import { ErrorState, LoadingState } from "./states";

/**
 * Right pane: everything known about the contact on the selected conversation — profile, custom
 * attributes, their recent conversations, and recent tracked events (RFC P0.5, §2.2 side panel).
 */
export function ContactPanel({
  contactId,
  onSelectConversation,
}: {
  contactId: string | null;
  onSelectConversation: (id: string) => void;
}) {
  const contact = useContact(contactId);
  const convs = useContactConversations(contactId);
  const events = useContactEvents(contactId);

  if (!contactId) return null;
  if (contact.isLoading) return <LoadingState label="Loading contact…" />;
  if (contact.isError) return <ErrorState error={contact.error} onRetry={() => void contact.refetch()} />;
  if (!contact.data) return null;

  const c = contact.data;
  const label = contactLabel(c);
  const customEntries = Object.entries(c.custom ?? {});

  return (
    <div className="h-full overflow-y-auto p-4" data-testid="contact-panel">
      <div className="flex flex-col items-center gap-2 pb-4 text-center">
        <Avatar label={initials(label)} className="h-12 w-12 text-sm" />
        <div>
          <p className="text-sm font-semibold">{label}</p>
          {c.email && <p className="text-xs text-muted-foreground">{c.email}</p>}
        </div>
        <Badge variant="muted">{c.kind}</Badge>
      </div>

      <Section title="Details">
        <Detail label="Email" value={c.email} />
        <Detail label="Phone" value={c.phone} />
        <Detail label="External ID" value={c.external_id} />
        <Detail label="First seen" value={c.created_at ? timeAgo(c.created_at) : null} />
        <Detail label="Last seen" value={c.last_seen_at ? timeAgo(c.last_seen_at) : null} />
      </Section>

      {customEntries.length > 0 && (
        <Section title="Custom attributes">
          {customEntries.map(([k, v]) => (
            <Detail key={k} label={k} value={String(v)} />
          ))}
        </Section>
      )}

      <Section title="Recent conversations">
        {convs.isLoading ? (
          <p className="text-xs text-muted-foreground">Loading…</p>
        ) : convs.data && convs.data.length > 0 ? (
          <ul className="space-y-1">
            {convs.data.map((cv) => (
              <li key={cv.id}>
                <button
                  onClick={() => onSelectConversation(cv.id)}
                  className="flex w-full items-center justify-between gap-2 rounded px-2 py-1 text-left text-xs hover:bg-accent"
                >
                  <span className="capitalize">{cv.state}</span>
                  <span className="text-muted-foreground">{timeAgo(cv.last_part_at)}</span>
                </button>
              </li>
            ))}
          </ul>
        ) : (
          <p className="text-xs text-muted-foreground">No other conversations.</p>
        )}
      </Section>

      <Section title="Recent events">
        {events.isLoading ? (
          <p className="text-xs text-muted-foreground">Loading…</p>
        ) : events.data && events.data.length > 0 ? (
          <ul className="space-y-1">
            {events.data.map((e, i) => (
              <li key={`${e.name}-${e.created_at}-${i}`} className="flex items-center justify-between gap-2 text-xs">
                <span className="truncate">{e.name}</span>
                <span className="shrink-0 text-muted-foreground">{timeAgo(e.created_at)}</span>
              </li>
            ))}
          </ul>
        ) : (
          <p className="text-xs text-muted-foreground">No events tracked yet.</p>
        )}
      </Section>
    </div>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="border-t border-border py-3">
      <p className="pb-2 text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
        {title}
      </p>
      {children}
    </div>
  );
}

function Detail({ label, value }: { label: string; value: string | null | undefined }) {
  if (!value) return null;
  return (
    <div className="flex items-start justify-between gap-3 py-0.5 text-xs">
      <span className="text-muted-foreground">{label}</span>
      <span className="max-w-[60%] truncate text-right">{value}</span>
    </div>
  );
}
