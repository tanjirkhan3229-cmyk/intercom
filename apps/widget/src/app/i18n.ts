/**
 * i18n-ready strings. Every user-facing string routes through `t(key)`; adding a locale is a
 * new dictionary + `setLocale`. No interpolation engine yet (YAGNI) — the few dynamic strings
 * take an argument.
 */

const en = {
  header_default: "Messages",
  greeting_default: "Hi there 👋 How can we help?",
  reply_time_prefix: "Typically replies in",
  composer_placeholder: "Write a reply…",
  send: "Send",
  attach: "Attach a file",
  start_conversation: "Send us a message",
  new_conversation: "New conversation",
  recent: "Recent conversations",
  back: "Back",
  delivered: "Delivered",
  sending: "Sending…",
  failed: "Not delivered — tap to retry",
  agent_typing: "typing…",
  rate_prompt: "How would you rate this conversation?",
  rate_thanks: "Thanks for your feedback!",
  closed_note: "This conversation is closed.",
  empty_thread: "No messages yet — say hello!",
  error_generic: "Something went wrong. Please try again.",
} as const;

export type Strings = typeof en;
export type StringKey = keyof Strings;

const dictionaries: Record<string, Strings> = { en };
let active: Strings = en;

export function setLocale(locale: string | undefined): void {
  if (!locale) return;
  const key = locale.slice(0, 2).toLowerCase();
  active = dictionaries[key] ?? en;
}

export function t(key: StringKey): string {
  return active[key];
}
