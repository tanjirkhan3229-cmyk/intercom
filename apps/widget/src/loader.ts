/**
 * Relay widget loader — the tiny snippet host pages embed (budget: ≤5 KB gz).
 *
 *   <script async src="https://cdn.relay.example/widget/v1/relay.js"></script>
 *   <script>relay('boot', { app_id: 'wrk_...', user: {...}, user_hash: '...' });</script>
 *
 * It injects a launcher bubble + an iframe hosting the messenger app, and brokers a small,
 * origin-checked postMessage protocol between the host page and the iframe:
 *
 *   loader → iframe : relay:boot {config}   ·  relay:visibility {open}
 *   iframe → loader : relay:ready            ·  relay:config {color, position}
 *                     relay:unread {count}   ·  relay:open / relay:close
 *
 * The iframe fetches workspace theme at boot and pushes it back (relay:config) so the launcher
 * matches without the host page hard-coding it. CSP-safe: no eval, no inline scripts — only DOM
 * APIs + CSSOM (which strict script-src/style-src do not block).
 */

type Theme = { color?: string; position?: "left" | "right" };

type BootConfig = {
  app_id: string;
  api_url?: string; // API origin (default: dev localhost)
  app_url?: string; // iframe app origin (default: dev localhost)
  user?: { external_id?: string; email?: string; name?: string };
  user_hash?: string;
  theme?: Theme;
};

type Command = ["boot", BootConfig] | ["open"] | ["close"] | ["toggle"] | ["shutdown"];

interface RelayQueue {
  (...args: Command): void;
  q?: Command[];
}

const DEFAULT_APP_URL = "http://localhost:5173";
const NS = "relay";
const Z = "2147483000";

// Our own <script> (captured synchronously). Served from a versioned dir (…/v1.2.0/relay.js) on
// the CDN, the iframe app sits right next to us at …/v1.2.0/index.html — so we default app_url
// to that, keeping the whole bundle version-pinned without the host page knowing the version.
const SELF = document.currentScript as HTMLScriptElement | null;

function defaultAppUrl(): string {
  try {
    return SELF?.src ? new URL("index.html", SELF.src).href : DEFAULT_APP_URL;
  } catch {
    return DEFAULT_APP_URL;
  }
}

let iframe: HTMLIFrameElement | null = null;
let launcher: HTMLButtonElement | null = null;
let badge: HTMLSpanElement | null = null;
let appOrigin = "";
let isOpen = false;
let booted = false;

function css(el: HTMLElement, styles: Partial<CSSStyleDeclaration>): void {
  Object.assign(el.style, styles);
}

function originOf(url: string): string {
  try {
    return new URL(url).origin;
  } catch {
    return url;
  }
}

function createLauncher(color: string, position: "left" | "right"): HTMLButtonElement {
  const btn = document.createElement("button");
  btn.setAttribute("aria-label", "Open messenger");
  btn.type = "button";
  css(btn, {
    position: "fixed",
    bottom: "20px",
    width: "60px",
    height: "60px",
    borderRadius: "50%",
    border: "none",
    background: color,
    color: "#fff",
    cursor: "pointer",
    zIndex: Z,
    boxShadow: "0 4px 16px rgba(0,0,0,0.18)",
    display: "flex",
    alignItems: "center",
    justifyContent: "center",
    transition: "transform 120ms ease",
  });
  btn.style[position] = "20px";
  // Inline SVG chat glyph — no external asset, no font, CSP-safe.
  btn.innerHTML =
    '<svg width="26" height="26" viewBox="0 0 24 24" fill="none" aria-hidden="true">' +
    '<path d="M4 4h16a1 1 0 0 1 1 1v11a1 1 0 0 1-1 1H8l-4 4V5a1 1 0 0 1 1-1z" fill="currentColor"/>' +
    "</svg>";

  badge = document.createElement("span");
  css(badge, {
    position: "absolute",
    top: "-2px",
    right: "-2px",
    minWidth: "20px",
    height: "20px",
    padding: "0 5px",
    borderRadius: "10px",
    background: "#ef4444",
    color: "#fff",
    font: "600 12px/20px system-ui, sans-serif",
    textAlign: "center",
    display: "none",
    boxSizing: "border-box",
  });
  btn.appendChild(badge);
  btn.addEventListener("click", () => toggle());
  return btn;
}

function createIframe(appUrl: string, position: "left" | "right"): HTMLIFrameElement {
  const frame = document.createElement("iframe");
  frame.src = appUrl;
  frame.title = "Relay messenger";
  frame.allow = "clipboard-write";
  css(frame, {
    position: "fixed",
    bottom: "92px",
    width: "384px",
    maxWidth: "calc(100vw - 40px)",
    height: "640px",
    maxHeight: "calc(100vh - 112px)",
    border: "none",
    borderRadius: "16px",
    boxShadow: "0 12px 40px rgba(0,0,0,0.16)",
    zIndex: Z,
    display: "none",
    colorScheme: "normal",
  });
  frame.style[position] = "20px";
  return frame;
}

function setOpen(next: boolean): void {
  if (!iframe) return;
  isOpen = next;
  iframe.style.display = isOpen ? "block" : "none";
  launcher?.setAttribute("aria-label", isOpen ? "Close messenger" : "Open messenger");
  post({ type: "relay:visibility", open: isOpen });
}

function toggle(force?: boolean): void {
  setOpen(force ?? !isOpen);
}

function post(msg: Record<string, unknown>): void {
  if (iframe?.contentWindow && appOrigin) {
    iframe.contentWindow.postMessage(msg, appOrigin);
  }
}

function setBadge(count: number): void {
  if (!badge) return;
  if (count > 0) {
    badge.textContent = count > 99 ? "99+" : String(count);
    badge.style.display = "block";
  } else {
    badge.style.display = "none";
  }
}

function reposition(position: "left" | "right"): void {
  const other = position === "left" ? "right" : "left";
  for (const el of [launcher, iframe]) {
    if (!el) continue;
    el.style[other] = "";
    el.style[position] = "20px";
  }
}

function onMessage(config: BootConfig): (ev: MessageEvent) => void {
  return (ev: MessageEvent) => {
    // Only trust messages from the iframe we injected.
    if (ev.origin !== appOrigin || ev.source !== iframe?.contentWindow) return;
    const data = ev.data as { type?: string; [k: string]: unknown };
    switch (data?.type) {
      case "relay:ready":
        post({ type: "relay:boot", config: { ...config, host_origin: location.origin } });
        break;
      case "relay:config": {
        const color = data.color as string | undefined;
        const position = data.position as "left" | "right" | undefined;
        if (color && launcher) launcher.style.background = color;
        if (position) reposition(position);
        break;
      }
      case "relay:unread":
        setBadge(Number(data.count) || 0);
        break;
      case "relay:open":
        setOpen(true);
        break;
      case "relay:close":
        setOpen(false);
        break;
    }
  };
}

function boot(config: BootConfig): void {
  if (booted) return;
  booted = true;
  const color = config.theme?.color ?? "#2563eb";
  const position = config.theme?.position ?? "right";
  const appUrl = config.app_url ?? defaultAppUrl();
  appOrigin = originOf(appUrl);

  launcher = createLauncher(color, position);
  iframe = createIframe(appUrl, position);
  document.body.appendChild(iframe);
  document.body.appendChild(launcher);
  window.addEventListener("message", onMessage(config));
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
    case "toggle":
      toggle();
      break;
    case "shutdown":
      iframe?.remove();
      launcher?.remove();
      iframe = launcher = badge = null;
      booted = isOpen = false;
      break;
  }
}

// Drain commands queued before this script loaded, then replace the stub.
const existing = (window as unknown as Record<string, RelayQueue | undefined>)[NS];
const queued = existing?.q ?? [];
const relay: RelayQueue = (...args: Command) => handle(args);
(window as unknown as Record<string, RelayQueue>)[NS] = relay;
for (const cmd of queued) handle(cmd);
