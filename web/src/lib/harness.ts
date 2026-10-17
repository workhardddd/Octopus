/**
 * Harness kinds: how to name one, and which one a new agent starts on.
 *
 * The set of kinds lives on the server — `GET /api/backends` lists the usable
 * ones with the default kind first — so nothing here decides what exists. That
 * keeps "which engines can I pick" and "which one is the default" in one place
 * instead of a literal list per component (dsh-harness.md §3.8).
 *
 * Vocabulary note: `dsh` is the DeepSeek Harness; the field that holds a kind
 * is `backend` (see CONTEXT.md).
 */

/** Display names. A kind with no entry is shown raw rather than hidden: a
 *  harness the UI hasn't learned about is still a fact about the agent. */
export const HARNESS_LABELS: Record<string, string> = {
  "claude-code": "Claude Code",
  codex: "Codex",
  dsh: "DeepSeek (DSH)",
};

/** Every kind this build knows how to name. Used only as a fallback so a form
 *  is not empty before `/api/backends` answers — availability still comes from
 *  the server. */
export const KNOWN_HARNESS_KINDS = ["dsh", "claude-code", "codex"];

export function harnessLabel(kind: string): string {
  return HARNESS_LABELS[kind] ?? kind;
}

/** The kind a new agent or session should start on.
 *
 * `/api/backends` puts the server's default first, so the list is the source of
 * truth for the default too. The fallback matches the store's pre-fetch
 * placeholder. */
export function defaultHarnessKind(available: string[]): string {
  return available[0] ?? KNOWN_HARNESS_KINDS[0];
}

/** The kinds to offer in a picker: what the server reports, or the known set
 *  while that request is in flight. */
export function harnessChoices(available: string[]): string[] {
  return available.length > 0 ? available : KNOWN_HARNESS_KINDS;
}
