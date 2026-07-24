/**
 * The P1.6 acceptance-scenario graph, as a fixture:
 *   new conversation → (outside office hours?) → collect email → hand to Aide →
 *   (still unresolved?) → route to Team X.
 *
 * Used by unit tests and available to the e2e mock as a sanity reference. Kept out of the app
 * bundle (only imported by tests / e2e support).
 */
import { OUTSIDE_OFFICE_HOURS_PREDICATE, type WorkflowGraph } from "../contract";

export function acceptanceGraph(): WorkflowGraph {
  return {
    nodes: [
      { id: "t", type: "trigger", trigger: "conversation.created", next: "c1", ui: { x: 80, y: 80 } },
      {
        id: "c1",
        type: "condition",
        predicate: OUTSIDE_OFFICE_HOURS_PREDICATE,
        true: "b1",
        false: "end",
        ui: { x: 380, y: 80 },
      },
      {
        id: "b1",
        type: "bot_step",
        bot: "collect",
        params: { prompt: "What's your email so we can follow up?", target: "contact", key: "email", next: "a1" },
        ui: { x: 680, y: 80 },
      },
      {
        id: "a1",
        type: "action",
        action: "hand_to_aide",
        params: {},
        next: "c2",
        ui: { x: 980, y: 80 },
      },
      {
        id: "c2",
        type: "condition",
        predicate: { op: "ne", field: "conversation.ai_status", value: "resolved" },
        true: "a2",
        false: "end",
        ui: { x: 1280, y: 80 },
      },
      {
        id: "a2",
        type: "action",
        action: "route_to_team",
        params: { team_id: "team_x" },
        next: "end",
        ui: { x: 1580, y: 80 },
      },
      { id: "end", type: "end", ui: { x: 1880, y: 80 } },
    ],
  };
}
