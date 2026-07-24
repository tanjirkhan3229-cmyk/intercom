"use client";

import { Input, Label, Select, Textarea } from "@/components/ui/primitives";
import { useTeams } from "@/lib/hooks";
import {
  ACTION_TYPES,
  ATTR_TARGETS,
  type ActionNode,
  type ActionType,
  type AttrTarget,
  type DurationParams,
} from "@/lib/workflows/contract";
import { ACTION_LABELS } from "../node-defs";
import { DurationField } from "./duration-field";

function defaultParams(action: ActionType): Record<string, unknown> {
  switch (action) {
    case "route_to_team":
      return { team_id: "" };
    case "add_tag":
      return { name: "" };
    case "set_attribute":
      return { target: "conversation", key: "", value: "" };
    case "snooze":
      return { seconds: 3600 };
    case "send_reply":
      return { body: "" };
    case "call_webhook":
      return { url: "", method: "POST" };
    default:
      return {};
  }
}

export function ActionConfig({
  node,
  onChange,
}: {
  node: ActionNode;
  onChange: (next: ActionNode) => void;
}) {
  const teams = useTeams();
  const params = node.params as Record<string, unknown>;

  const setAction = (action: ActionType) =>
    onChange({ id: node.id, type: "action", action, params: defaultParams(action), next: node.next, ui: node.ui } as ActionNode);

  const patchParams = (patch: Record<string, unknown>) =>
    onChange({ ...node, params: { ...params, ...patch } } as ActionNode);

  return (
    <div className="flex flex-col gap-3">
      <div>
        <Label>Action</Label>
        <Select
          data-testid="action-type"
          value={node.action}
          onChange={(e) => setAction(e.target.value as ActionType)}
        >
          {ACTION_TYPES.map((a) => (
            <option key={a} value={a}>
              {ACTION_LABELS[a]}
            </option>
          ))}
        </Select>
      </div>

      {node.action === "route_to_team" && (
        <div>
          <Label>Team</Label>
          <Select
            data-testid="route-team"
            value={typeof params.team_id === "string" ? params.team_id : ""}
            onChange={(e) => patchParams({ team_id: e.target.value })}
          >
            <option value="" disabled>
              Select a team…
            </option>
            {(teams.data ?? []).map((t) => (
              <option key={t.id} value={t.id}>
                {t.name}
              </option>
            ))}
          </Select>
        </div>
      )}

      {node.action === "assign" && (
        <>
          <div>
            <Label>Teammate id (optional)</Label>
            <Input
              value={typeof params.assignee_id === "string" ? params.assignee_id : ""}
              onChange={(e) => patchParams({ assignee_id: e.target.value })}
            />
          </div>
          <div>
            <Label>Team (optional)</Label>
            <Select
              value={typeof params.team_id === "string" ? params.team_id : ""}
              onChange={(e) => patchParams({ team_id: e.target.value })}
            >
              <option value="">—</option>
              {(teams.data ?? []).map((t) => (
                <option key={t.id} value={t.id}>
                  {t.name}
                </option>
              ))}
            </Select>
          </div>
        </>
      )}

      {node.action === "add_tag" && (
        <div>
          <Label>Tag name</Label>
          <Input
            data-testid="tag-name"
            value={typeof params.name === "string" ? params.name : ""}
            onChange={(e) => patchParams({ name: e.target.value })}
          />
        </div>
      )}

      {node.action === "set_attribute" && (
        <>
          <div>
            <Label>Target</Label>
            <Select
              value={typeof params.target === "string" ? params.target : "conversation"}
              onChange={(e) => patchParams({ target: e.target.value as AttrTarget })}
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
              value={typeof params.key === "string" ? params.key : ""}
              onChange={(e) => patchParams({ key: e.target.value })}
            />
          </div>
          <div>
            <Label>Value</Label>
            <Input
              value={typeof params.value === "string" ? params.value : String(params.value ?? "")}
              onChange={(e) => patchParams({ value: e.target.value })}
            />
          </div>
        </>
      )}

      {node.action === "send_reply" && (
        <div>
          <Label>Message</Label>
          <Textarea
            data-testid="reply-body"
            rows={4}
            value={typeof params.body === "string" ? params.body : ""}
            onChange={(e) => patchParams({ body: e.target.value })}
          />
        </div>
      )}

      {node.action === "snooze" && (
        <DurationField
          value={(params as DurationParams) ?? { seconds: 3600 }}
          onChange={(d) => onChange({ ...node, params: d } as ActionNode)}
        />
      )}

      {node.action === "call_webhook" && (
        <>
          <div>
            <Label>URL (POST)</Label>
            <Input
              data-testid="webhook-url"
              placeholder="https://api.example.com/hook"
              value={typeof params.url === "string" ? params.url : ""}
              onChange={(e) => patchParams({ url: e.target.value })}
            />
          </div>
          <p className="text-[11px] text-muted-foreground">
            Delivered as a signed POST through the SSRF-guarded proxy.
          </p>
        </>
      )}

      {(node.action === "close" || node.action === "hand_to_aide" || node.action === "apply_sla") && (
        <p className="text-xs text-muted-foreground">
          {node.action === "hand_to_aide"
            ? "Hands the conversation to Aide (the AI agent). No configuration needed."
            : node.action === "close"
              ? "Closes the conversation. No configuration needed."
              : "Applies the workspace SLA policy (flag-gated)."}
        </p>
      )}
    </div>
  );
}
