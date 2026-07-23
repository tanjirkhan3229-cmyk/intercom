import type { Contact, Session } from "./types";
import { contactLabel } from "./format";

/**
 * Interpolate saved-reply variables (RFC P0.5 macros). Supports `{{contact.*}}` and `{{agent.*}}`
 * placeholders; unknown variables are left intact so a typo is visible rather than silently blank.
 */
export function interpolateMacro(
  body: string,
  ctx: { contact?: Contact | null; session?: Session | null },
): string {
  const contactName = ctx.contact ? contactLabel(ctx.contact) : "";
  const firstName = contactName.split(/\s+/)[0] ?? "";
  const agentName = ctx.session?.admin.name ?? "";
  const vars: Record<string, string> = {
    "contact.name": contactName,
    "contact.first_name": firstName,
    "contact.email": ctx.contact?.email ?? "",
    "agent.name": agentName,
    "agent.first_name": agentName.split(/\s+/)[0] ?? "",
    "workspace.name": ctx.session?.workspace.name ?? "",
  };
  return body.replace(/\{\{\s*([\w.]+)\s*\}\}/g, (whole, key: string) =>
    key in vars ? vars[key]! : whole,
  );
}
