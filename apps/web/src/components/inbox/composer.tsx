"use client";

import { Paperclip, Send, StickyNote, X } from "lucide-react";
import * as React from "react";
import { useApi, useAuth } from "@/lib/auth";
import { useContact, useReply, useNote, useSavedReplies } from "@/lib/hooks";
import { interpolateMacro } from "@/lib/macros";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/primitives";
import { cn } from "@/lib/utils";
import type { Attachment, SavedReply } from "@/lib/types";

export interface ComposerHandle {
  focusReply: () => void;
  focusNote: () => void;
}

type Mode = "reply" | "note";

/**
 * Bottom-of-thread composer (RFC P0.5): reply vs note toggle, ⌘/Ctrl-Enter to send, `/`-triggered
 * macro picker with variable interpolation, and presigned-S3 attachment upload. Sends are handled
 * optimistically by the mutation hooks (reconciled by part id).
 */
export const Composer = React.forwardRef<ComposerHandle, { conversationId: string; contactId: string }>(
  function Composer({ conversationId, contactId }, ref) {
    const api = useApi();
    const { session } = useAuth();
    const contact = useContact(contactId);
    const savedReplies = useSavedReplies();
    const reply = useReply(conversationId);
    const note = useNote(conversationId);

    const [mode, setMode] = React.useState<Mode>("reply");
    const [text, setText] = React.useState("");
    const [attachments, setAttachments] = React.useState<Attachment[]>([]);
    const [uploading, setUploading] = React.useState(0);
    const [macroIndex, setMacroIndex] = React.useState(0);
    const textareaRef = React.useRef<HTMLTextAreaElement>(null);
    const fileRef = React.useRef<HTMLInputElement>(null);

    React.useImperativeHandle(ref, () => ({
      focusReply: () => {
        setMode("reply");
        textareaRef.current?.focus();
      },
      focusNote: () => {
        setMode("note");
        textareaRef.current?.focus();
      },
    }));

    // Reset when switching conversations.
    React.useEffect(() => {
      setText("");
      setAttachments([]);
    }, [conversationId]);

    const macroQuery = mode === "reply" && text.startsWith("/") ? text.slice(1).toLowerCase() : null;
    const macroMatches: SavedReply[] =
      macroQuery !== null
        ? (savedReplies.data ?? []).filter(
            (m) =>
              m.shortcut.toLowerCase().includes(macroQuery) ||
              m.title.toLowerCase().includes(macroQuery),
          )
        : [];
    const macroOpen = macroMatches.length > 0;

    React.useEffect(() => setMacroIndex(0), [macroQuery]);

    const applyMacro = (m: SavedReply) => {
      setText(interpolateMacro(m.body, { contact: contact.data, session }));
      textareaRef.current?.focus();
    };

    const isBusy = reply.isPending || note.isPending || uploading > 0;
    const canSend = (text.trim().length > 0 || attachments.length > 0) && !isBusy;

    const send = () => {
      if (!canSend) return;
      const body = text.trim();
      if (mode === "reply") {
        reply.mutate({ body: body || "(attachment)", attachments });
      } else {
        note.mutate({ body });
      }
      setText("");
      setAttachments([]);
    };

    const onKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
      if (macroOpen) {
        if (e.key === "ArrowDown") {
          e.preventDefault();
          setMacroIndex((i) => Math.min(i + 1, macroMatches.length - 1));
          return;
        }
        if (e.key === "ArrowUp") {
          e.preventDefault();
          setMacroIndex((i) => Math.max(i - 1, 0));
          return;
        }
        if (e.key === "Enter" && !e.shiftKey) {
          e.preventDefault();
          const chosen = macroMatches[macroIndex] ?? macroMatches[0];
          if (chosen) applyMacro(chosen);
          return;
        }
        if (e.key === "Escape") {
          setText("");
          return;
        }
      }
      if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
        e.preventDefault();
        send();
      }
    };

    const onPickFiles = async (files: FileList | null) => {
      if (!files || files.length === 0) return;
      setUploading((n) => n + files.length);
      for (const file of Array.from(files)) {
        try {
          const { key, upload_url } = await api.presignUpload(
            file.name,
            file.type || "application/octet-stream",
          );
          const res = await fetch(upload_url, {
            method: "PUT",
            body: file,
            headers: { "Content-Type": file.type || "application/octet-stream" },
          });
          if (!res.ok) throw new Error(`upload failed (${res.status})`);
          setAttachments((a) => [
            ...a,
            { key, name: file.name, content_type: file.type, size: file.size },
          ]);
        } catch {
          // Surface nothing destructive; the agent can retry. (A toast lands with P0.9 UX.)
        } finally {
          setUploading((n) => Math.max(0, n - 1));
        }
      }
      if (fileRef.current) fileRef.current.value = "";
    };

    return (
      <div className="border-t border-border bg-background">
        <div className="flex items-center gap-1 px-3 pt-2">
          <ModeTab active={mode === "reply"} onClick={() => setMode("reply")} icon={<Send className="h-3.5 w-3.5" />} label="Reply" />
          <ModeTab active={mode === "note"} onClick={() => setMode("note")} icon={<StickyNote className="h-3.5 w-3.5" />} label="Note" />
        </div>

        <div className={cn("relative m-3 mt-2 rounded-md border", mode === "note" ? "border-amber-300 bg-amber-50/60 dark:bg-amber-950/20" : "border-input")}>
          {macroOpen && (
            <ul className="absolute bottom-full left-0 z-20 mb-1 max-h-56 w-full overflow-y-auto rounded-md border border-border bg-popover p-1 shadow-md">
              {macroMatches.map((m, i) => (
                <li key={m.id}>
                  <button
                    type="button"
                    onMouseEnter={() => setMacroIndex(i)}
                    onClick={() => applyMacro(m)}
                    className={cn(
                      "flex w-full flex-col rounded-sm px-2 py-1.5 text-left text-sm",
                      i === macroIndex ? "bg-accent text-accent-foreground" : "hover:bg-accent/60",
                    )}
                  >
                    <span className="font-medium">
                      /{m.shortcut} <span className="text-muted-foreground">· {m.title}</span>
                    </span>
                    <span className="truncate text-xs text-muted-foreground">{m.body}</span>
                  </button>
                </li>
              ))}
            </ul>
          )}

          <Textarea
            ref={textareaRef}
            value={text}
            onChange={(e) => setText(e.target.value)}
            onKeyDown={onKeyDown}
            placeholder={
              mode === "reply"
                ? "Write a reply…  ⌘⏎ to send  ·  / for macros"
                : "Add an internal note (not visible to the contact)…  ⌘⏎ to send"
            }
            className="min-h-[80px] resize-none border-0 bg-transparent shadow-none focus-visible:ring-0"
            data-testid="composer-input"
          />

          {attachments.length > 0 && (
            <div className="flex flex-wrap gap-1.5 px-3 pb-2">
              {attachments.map((a) => (
                <span key={a.key} className="flex items-center gap-1 rounded bg-muted px-2 py-0.5 text-xs">
                  <Paperclip className="h-3 w-3" />
                  <span className="max-w-[140px] truncate">{a.name}</span>
                  <button
                    onClick={() => setAttachments((list) => list.filter((x) => x.key !== a.key))}
                    className="text-muted-foreground hover:text-foreground"
                    aria-label={`Remove ${a.name}`}
                  >
                    <X className="h-3 w-3" />
                  </button>
                </span>
              ))}
            </div>
          )}

          <div className="flex items-center justify-between border-t border-border/60 px-2 py-1.5">
            <button
              type="button"
              onClick={() => fileRef.current?.click()}
              className="rounded p-1.5 text-muted-foreground hover:bg-accent hover:text-foreground"
              title="Attach files"
              aria-label="Attach files"
            >
              <Paperclip className="h-4 w-4" />
            </button>
            <input
              ref={fileRef}
              type="file"
              multiple
              className="hidden"
              onChange={(e) => void onPickFiles(e.target.files)}
            />
            <Button size="sm" onClick={send} disabled={!canSend} data-testid="composer-send">
              {uploading > 0 ? "Uploading…" : mode === "reply" ? "Send reply" : "Add note"}
            </Button>
          </div>
        </div>
      </div>
    );
  },
);

function ModeTab({
  active,
  onClick,
  icon,
  label,
}: {
  active: boolean;
  onClick: () => void;
  icon: React.ReactNode;
  label: string;
}) {
  return (
    <button
      onClick={onClick}
      className={cn(
        "flex items-center gap-1.5 rounded-t-md border-b-2 px-3 py-1.5 text-xs font-medium transition-colors",
        active
          ? "border-primary text-foreground"
          : "border-transparent text-muted-foreground hover:text-foreground",
      )}
    >
      {icon}
      {label}
    </button>
  );
}
