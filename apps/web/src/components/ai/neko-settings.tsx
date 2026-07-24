"use client";

import * as React from "react";
import { useAiSettings, useNekoUsage, useUpdateAiSettings } from "@/lib/ai-hooks";
import { Button } from "@/components/ui/button";
import { Input, Textarea, Badge, Spinner } from "@/components/ui/primitives";
import { LoadingState, ErrorState } from "@/components/inbox/states";
import { cn } from "@/lib/utils";
import type { AiSettingsInput, NekoTone, OfficeHoursBehavior } from "@/lib/types";

// The answer-length presets map to the backend token budget (RFC-003 §9 context trimming).
const LENGTH_PRESETS: { label: string; tokens: number }[] = [
  { label: "Short", tokens: 200 },
  { label: "Medium", tokens: 400 },
  { label: "Long", tokens: 800 },
];
const TONES: NekoTone[] = ["friendly", "neutral", "formal"];
// Which retrieval sources Neko may ground on (RFC-002 §5.5). None checked ⇒ all sources.
const SOURCE_KINDS = ["article", "pdf", "url", "snippet", "custom_answer"];

/**
 * Neko settings (RFC-003 §5-6, §9): the per-workspace enable + persona/tone, answer length,
 * grounding-gate slider, handoff rules, source scope, and monthly spend cap. Explicit Save, since
 * these are workspace-wide agent controls (mirrors the Help Center settings form).
 */
export function NekoSettings() {
  const query = useAiSettings();
  const usage = useNekoUsage();
  const update = useUpdateAiSettings();

  const [enabled, setEnabled] = React.useState(false);
  const [chatOn, setChatOn] = React.useState(true);
  const [tone, setTone] = React.useState<NekoTone>("neutral");
  const [persona, setPersona] = React.useState("");
  const [answerTokens, setAnswerTokens] = React.useState(400);
  const [grounding, setGrounding] = React.useState(0.1);
  const [intents, setIntents] = React.useState("");
  const [officeHours, setOfficeHours] = React.useState<OfficeHoursBehavior>("answer");
  const [scope, setScope] = React.useState<string[]>([]);
  const [cap, setCap] = React.useState("");
  const hydrated = React.useRef(false);

  const cfg = query.data;
  React.useEffect(() => {
    if (!cfg || hydrated.current) return;
    hydrated.current = true;
    setEnabled(cfg.enabled);
    setChatOn(cfg.channels.includes("chat"));
    setTone(cfg.tone);
    setPersona(cfg.persona ?? "");
    setAnswerTokens(cfg.answer_max_tokens);
    setGrounding(cfg.grounding_threshold);
    setIntents((cfg.always_handoff_intents ?? []).join("\n"));
    setOfficeHours(cfg.office_hours_behavior);
    setScope(cfg.source_kinds ?? []);
    setCap(cfg.monthly_spend_cap_usd == null ? "" : String(cfg.monthly_spend_cap_usd));
  }, [cfg]);

  const onSave = () => {
    const parsedCap = cap.trim() === "" ? null : Number(cap);
    const input: AiSettingsInput = {
      enabled,
      channels: chatOn ? ["chat"] : [],
      tone,
      persona: persona.trim() || null,
      answer_max_tokens: answerTokens,
      grounding_threshold: grounding,
      always_handoff_intents: intents
        .split("\n")
        .map((s) => s.trim())
        .filter(Boolean),
      office_hours_behavior: officeHours,
      // None checked ⇒ all sources (backend null); otherwise the restricted set.
      source_kinds: scope.length === 0 ? null : scope,
      monthly_spend_cap_usd: parsedCap,
    };
    update.mutate(input);
  };

  if (query.isLoading) return <LoadingState label="Loading Neko settings…" className="h-40" />;
  if (query.isError) {
    return <ErrorState error={query.error} onRetry={() => void query.refetch()} className="h-40" />;
  }

  const capInvalid = cap.trim() !== "" && (Number.isNaN(Number(cap)) || Number(cap) < 0);

  return (
    <form
      className="flex max-w-xl flex-col gap-6"
      onSubmit={(e) => {
        e.preventDefault();
        onSave();
      }}
    >
      <UsageCard usage={usage.data} />

      <Field label="Status">
        <Toggle
          checked={enabled}
          onChange={setEnabled}
          label={enabled ? "Neko is answering customers" : "Neko is off"}
        />
      </Field>

      <Field label="Channels" hint="Chat only in this phase.">
        <Toggle checked={chatOn} onChange={setChatOn} label="Chat (Messenger)" />
      </Field>

      <Field label="Tone">
        <Select value={tone} onChange={(v) => setTone(v as NekoTone)} options={TONES} />
      </Field>

      <Field label="Custom guidance" hint="Extra persona instructions folded into every answer.">
        <Textarea
          value={persona}
          onChange={(e) => setPersona(e.target.value)}
          placeholder="e.g. Always mention our 30-day guarantee when relevant."
          rows={3}
        />
      </Field>

      <Field label="Answer length">
        <div className="flex gap-1">
          {LENGTH_PRESETS.map((p) => (
            <PresetButton
              key={p.tokens}
              label={p.label}
              active={answerTokens === p.tokens}
              onClick={() => setAnswerTokens(p.tokens)}
            />
          ))}
        </div>
      </Field>

      <Field
        label="Grounding gate"
        hint="How much evidence Neko needs before answering. Higher = more conservative (hands off sooner)."
      >
        <div className="flex items-center gap-3">
          <input
            type="range"
            min={0}
            max={1}
            step={0.01}
            value={grounding}
            onChange={(e) => setGrounding(Number(e.target.value))}
            aria-label="Grounding conservatism"
            className="h-2 flex-1 cursor-pointer accent-primary"
          />
          <span className="w-24 text-right text-xs text-muted-foreground">
            {grounding < 0.34 ? "Eager" : grounding < 0.67 ? "Balanced" : "Conservative"} (
            {grounding.toFixed(2)})
          </span>
        </div>
      </Field>

      <Field label="Always hand off" hint="One intent phrase per line — these route straight to a human.">
        <Textarea
          value={intents}
          onChange={(e) => setIntents(e.target.value)}
          placeholder={"cancel my account\nspeak to billing"}
          rows={3}
        />
      </Field>

      <Field label="Outside office hours">
        <Select
          value={officeHours}
          onChange={(v) => setOfficeHours(v as OfficeHoursBehavior)}
          options={["answer", "handoff"]}
        />
      </Field>

      <Field label="Source scope" hint="Which knowledge Neko may use. Leave all off to use everything.">
        <div className="flex flex-wrap gap-2">
          {SOURCE_KINDS.map((k) => (
            <Chip
              key={k}
              label={k}
              active={scope.includes(k)}
              onClick={() =>
                setScope((prev) =>
                  prev.includes(k) ? prev.filter((x) => x !== k) : [...prev, k],
                )
              }
            />
          ))}
        </div>
      </Field>

      <Field label="Monthly spend cap (USD)" hint="Past this, Neko routes to humans. Blank = no cap.">
        <Input
          value={cap}
          onChange={(e) => setCap(e.target.value)}
          inputMode="decimal"
          placeholder="e.g. 500"
          className="max-w-[10rem]"
        />
        {capInvalid && <p className="mt-1 text-xs text-destructive">Enter a non-negative number.</p>}
      </Field>

      <div className="flex items-center gap-3 border-t border-border pt-4">
        <Button type="submit" size="sm" disabled={update.isPending || capInvalid}>
          {update.isPending ? <Spinner className="h-3.5 w-3.5" /> : "Save settings"}
        </Button>
        {update.isSuccess && <span className="text-xs text-muted-foreground">Saved</span>}
        {update.isError && <span className="text-xs text-destructive">Save failed</span>}
      </div>
    </form>
  );
}

function UsageCard({ usage }: { usage: import("@/lib/types").NekoUsage | undefined }) {
  if (!usage) return null;
  return (
    <div className="rounded-lg border border-border bg-muted/30 p-4">
      <div className="flex items-center justify-between">
        <p className="text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
          This month
        </p>
        {usage.over_cap && <Badge className="bg-destructive/10 text-destructive">Over cap</Badge>}
      </div>
      <div className="mt-2 flex items-baseline gap-4">
        <div>
          <p className="text-2xl font-semibold">{usage.month_resolutions}</p>
          <p className="text-xs text-muted-foreground">resolutions</p>
        </div>
        <div>
          <p className="text-2xl font-semibold">${usage.month_spend_usd.toFixed(2)}</p>
          <p className="text-xs text-muted-foreground">
            spend{usage.monthly_spend_cap_usd != null && ` / $${usage.monthly_spend_cap_usd} cap`}
          </p>
        </div>
      </div>
    </div>
  );
}

function Field({
  label,
  hint,
  children,
}: {
  label: string;
  hint?: string;
  children: React.ReactNode;
}) {
  return (
    <div className="flex flex-col gap-1.5">
      <label className="text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
        {label}
      </label>
      {hint && <p className="text-xs text-muted-foreground">{hint}</p>}
      {children}
    </div>
  );
}

function Toggle({
  checked,
  onChange,
  label,
}: {
  checked: boolean;
  onChange: (v: boolean) => void;
  label: string;
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      onClick={() => onChange(!checked)}
      className="flex items-center gap-2 text-sm"
    >
      <span
        className={cn(
          "relative inline-flex h-5 w-9 shrink-0 items-center rounded-full transition-colors",
          checked ? "bg-primary" : "bg-muted-foreground/30",
        )}
      >
        <span
          className={cn(
            "inline-block h-4 w-4 transform rounded-full bg-background transition-transform",
            checked ? "translate-x-4" : "translate-x-0.5",
          )}
        />
      </span>
      <span className="text-muted-foreground">{label}</span>
    </button>
  );
}

function Select({
  value,
  onChange,
  options,
}: {
  value: string;
  onChange: (v: string) => void;
  options: string[];
}) {
  return (
    <select
      value={value}
      onChange={(e) => onChange(e.target.value)}
      className="h-9 max-w-[12rem] rounded-md border border-input bg-background px-3 text-sm capitalize shadow-sm focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
    >
      {options.map((o) => (
        <option key={o} value={o} className="capitalize">
          {o}
        </option>
      ))}
    </select>
  );
}

function PresetButton({
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
      type="button"
      onClick={onClick}
      aria-pressed={active}
      className={cn(
        "rounded-md px-3 py-1 text-xs font-medium transition-colors",
        active ? "bg-accent text-accent-foreground" : "text-muted-foreground hover:bg-accent/50",
      )}
    >
      {label}
    </button>
  );
}

function Chip({ label, active, onClick }: { label: string; active: boolean; onClick: () => void }) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-pressed={active}
      className={cn(
        "rounded-full border px-3 py-1 text-xs font-medium transition-colors",
        active
          ? "border-primary bg-primary/10 text-primary"
          : "border-input text-muted-foreground hover:bg-accent/50",
      )}
    >
      {label}
    </button>
  );
}
