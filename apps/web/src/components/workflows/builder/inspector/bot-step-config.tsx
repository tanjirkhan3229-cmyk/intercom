"use client";

import { Button } from "@/components/ui/button";
import { Input, Label, Select, Textarea } from "@/components/ui/primitives";
import {
  ATTR_TARGETS,
  BOT_KINDS,
  type AttrTarget,
  type BotKind,
  type BotOption,
  type BotStepNode,
} from "@/lib/workflows/contract";
import { newNodeId } from "../node-defs";

const BOT_LABELS: Record<BotKind, string> = {
  collect: "Collect a reply",
  ask_buttons: "Ask with buttons",
  disambiguate: "Disambiguate",
};

function defaultBotParams(kind: BotKind): Record<string, unknown> {
  if (kind === "collect") return { prompt: "", target: "contact", key: "", next: "" };
  return { prompt: "", options: [{ id: newNodeId(), label: "Yes", value: "yes", next: "" }] };
}

export function BotStepConfig({
  node,
  onChange,
}: {
  node: BotStepNode;
  onChange: (next: BotStepNode) => void;
}) {
  const params = node.params as unknown as Record<string, unknown>;
  const prompt = typeof params.prompt === "string" ? params.prompt : "";

  const setKind = (bot: BotKind) =>
    onChange({
      id: node.id,
      type: "bot_step",
      bot,
      params: defaultBotParams(bot),
      ui: node.ui,
    } as unknown as BotStepNode);

  const patch = (p: Record<string, unknown>) =>
    onChange({ ...node, params: { ...params, ...p } } as unknown as BotStepNode);

  const options: BotOption[] = Array.isArray(params.options) ? (params.options as BotOption[]) : [];
  const setOptions = (next: BotOption[]) => patch({ options: next });

  return (
    <div className="flex flex-col gap-3">
      <div>
        <Label>Bot step</Label>
        <Select
          data-testid="bot-kind"
          value={node.bot}
          onChange={(e) => setKind(e.target.value as BotKind)}
        >
          {BOT_KINDS.map((k) => (
            <option key={k} value={k}>
              {BOT_LABELS[k]}
            </option>
          ))}
        </Select>
      </div>

      <div>
        <Label>Prompt shown to the customer</Label>
        <Textarea
          data-testid="bot-prompt"
          rows={3}
          value={prompt}
          onChange={(e) => patch({ prompt: e.target.value })}
        />
      </div>

      {node.bot === "collect" ? (
        <>
          <div>
            <Label>Store the reply on</Label>
            <Select
              data-testid="collect-target"
              value={typeof params.target === "string" ? params.target : "contact"}
              onChange={(e) => patch({ target: e.target.value as AttrTarget })}
            >
              {ATTR_TARGETS.map((t) => (
                <option key={t} value={t}>
                  {t}
                </option>
              ))}
            </Select>
          </div>
          <div>
            <Label>Attribute key</Label>
            <Input
              data-testid="collect-key"
              placeholder="email"
              value={typeof params.key === "string" ? params.key : ""}
              onChange={(e) => patch({ key: e.target.value })}
            />
          </div>
        </>
      ) : (
        <div className="flex flex-col gap-2">
          <Label>Options (each becomes a branch)</Label>
          {options.map((opt, i) => (
            <div key={i} className="flex items-center gap-2" data-testid="bot-option">
              <Input
                aria-label="Option label"
                placeholder="Label"
                value={opt.label}
                onChange={(e) =>
                  setOptions(options.map((o, idx) => (idx === i ? { ...o, label: e.target.value } : o)))
                }
              />
              <Input
                aria-label="Option value"
                placeholder="value"
                value={opt.value}
                onChange={(e) =>
                  setOptions(options.map((o, idx) => (idx === i ? { ...o, value: e.target.value } : o)))
                }
              />
              <Button
                type="button"
                variant="ghost"
                size="icon"
                aria-label="Remove option"
                onClick={() => setOptions(options.filter((_, idx) => idx !== i))}
              >
                ✕
              </Button>
            </div>
          ))}
          <Button
            type="button"
            variant="outline"
            size="sm"
            data-testid="bot-add-option"
            onClick={() => setOptions([...options, { id: newNodeId(), label: "", value: "", next: "" }])}
          >
            + Option
          </Button>
        </div>
      )}
    </div>
  );
}
