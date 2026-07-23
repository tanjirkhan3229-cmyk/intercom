import { revalidatePath } from "next/cache";

/**
 * On-demand ISR hook. The backend's `help-center-revalidate` outbox consumer POSTs here
 * whenever an article/collection is published or updated, so the hosted site reflects live
 * changes within seconds instead of waiting for the 60s time-based backstop.
 *
 * Auth: a shared secret in the `x-relay-revalidate-secret` header (never a user token).
 */
export async function POST(req: Request) {
  const secret = process.env.HELP_CENTER_REVALIDATE_SECRET ?? "dev-help-center-revalidate-secret";
  const provided = req.headers.get("x-relay-revalidate-secret");

  if (provided !== secret) {
    return Response.json({ error: "unauthorized" }, { status: 401 });
  }

  let paths: string[] = [];
  try {
    const body = (await req.json()) as { paths?: unknown };
    if (Array.isArray(body.paths)) {
      paths = body.paths.filter((p): p is string => typeof p === "string");
    }
  } catch {
    return Response.json({ error: "invalid json body" }, { status: 400 });
  }

  for (const path of paths) {
    revalidatePath(path);
  }

  return Response.json({ revalidated: true, paths });
}
