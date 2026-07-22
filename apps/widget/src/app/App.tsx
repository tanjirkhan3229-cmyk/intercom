import { useState } from "preact/hooks";

/**
 * Placeholder iframe app. The real messenger (launcher, conversation list, thread,
 * composer, typing/read state, ratings) lands in P0.6. Kept dependency-light to hold the
 * 50 KB gz bundle budget.
 */
export function App() {
  const [open] = useState(true);
  return (
    <div class="relay-panel" data-open={open}>
      <header class="relay-header">
        <span>Relay</span>
      </header>
      <div class="relay-body">
        <p>How can we help?</p>
        <p class="relay-muted">Messenger placeholder — full widget arrives in P0.6.</p>
      </div>
      <footer class="relay-footer">
        <input class="relay-input" placeholder="Write a message…" disabled />
      </footer>
    </div>
  );
}
