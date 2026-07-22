/**
 * Relay widget loader — the tiny snippet host pages embed (target: ≤5 KB gz).
 *
 * Usage on a customer page:
 *   <script async src="https://cdn.relay.example/widget/v1/relay.js"></script>
 *   <script>relay('boot', { app_id: 'wrk_...', user: {...}, user_hash: '...' });</script>
 *
 * It injects a launcher bubble + an iframe hosting the messenger app. CSP-safe: no eval,
 * no inline scripts, only DOM APIs. Boot verification (user_hash HMAC) + versioned CDN
 * rollout land in P0.6; this is the placeholder loader.
 */

type BootConfig = {
  app_id: string;
  app_url?: string;
  user?: { external_id?: string; email?: string; name?: string };
  user_hash?: string;
  theme?: { color?: string; position?: "left" | "right" };
};

type Command = ["boot", BootConfig] | ["open"] | ["close"] | ["shutdown"];

interface RelayQueue {
  (...args: Command): void;
  q?: Command[];
}

const DEFAULT_APP_URL = "http://localhost:5173";
const NS = "relay";

let iframe: HTMLIFrameElement | null = null;
let launcher: HTMLButtonElement | null = null;
let booted = false;

function css(el: HTMLElement, styles: Partial<CSSStyleDeclaration>): void {
  Object.assign(el.style, styles);
}

function createLauncher(color: string, position: "left" | "right"): HTMLButtonElement {
  const btn = document.createElement("button");
  btn.setAttribute("aria-label", "Open messenger");
  css(btn, {
    position: "fixed",
    bottom: "20px",
    width: "56px",
    height: "56px",
    borderRadius: "50%",
    border: "none",
    background: color,
    color: "#fff",
    cursor: "pointer",
    zIndex: "2147483000",
    boxShadow: "0 4px 12px rgba(0,0,0,0.15)",
  });
  btn.style[position] = "20px";
  btn.textContent = "💬";
  btn.addEventListener("click", () => toggle());
  return btn;
}

function createIframe(appUrl: string, position: "left" | "right"): HTMLIFrameElement {
  const frame = document.createElement("iframe");
  frame.src = appUrl;
  frame.title = "Relay messenger";
  css(frame, {
    position: "fixed",
    bottom: "88px",
    width: "380px",
    height: "600px",
    maxHeight: "calc(100vh - 108px)",
    border: "none",
    borderRadius: "16px",
    boxShadow: "0 8px 30px rgba(0,0,0,0.2)",
    zIndex: "2147483000",
    display: "none",
  });
  frame.style[position] = "20px";
  return frame;
}

function toggle(force?: boolean): void {
  if (!iframe) return;
  const show = force ?? iframe.style.display === "none";
  iframe.style.display = show ? "block" : "none";
}

function boot(config: BootConfig): void {
  if (booted) return;
  booted = true;
  const color = config.theme?.color ?? "#2563eb";
  const position = config.theme?.position ?? "right";
  const appUrl = config.app_url ?? DEFAULT_APP_URL;

  launcher = createLauncher(color, position);
  iframe = createIframe(appUrl, position);
  document.body.appendChild(iframe);
  document.body.appendChild(launcher);

  // Hand the boot config to the iframe app once it signals ready (P0.6 fleshes this out).
  window.addEventListener("message", (ev: MessageEvent) => {
    if (ev.source === iframe?.contentWindow && ev.data?.type === "relay:ready") {
      iframe?.contentWindow?.postMessage({ type: "relay:boot", config }, appUrl);
    }
  });
}

function handle(cmd: Command): void {
  switch (cmd[0]) {
    case "boot":
      boot(cmd[1]);
      break;
    case "open":
      toggle(true);
      break;
    case "close":
      toggle(false);
      break;
    case "shutdown":
      iframe?.remove();
      launcher?.remove();
      iframe = launcher = null;
      booted = false;
      break;
  }
}

// Drain any commands queued before this script loaded, then replace the stub.
const existing = (window as unknown as Record<string, RelayQueue | undefined>)[NS];
const queued = existing?.q ?? [];
const relay: RelayQueue = (...args: Command) => handle(args);
(window as unknown as Record<string, RelayQueue>)[NS] = relay;
for (const cmd of queued) handle(cmd);
