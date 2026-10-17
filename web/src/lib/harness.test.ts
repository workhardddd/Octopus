import { describe, expect, it } from "vitest";

import {
  HARNESS_LABELS,
  KNOWN_HARNESS_KINDS,
  defaultHarnessKind,
  harnessChoices,
  harnessLabel,
} from "./harness";

describe("harness labels", () => {
  it("names the kinds this build knows", () => {
    expect(harnessLabel("claude-code")).toBe("Claude Code");
    expect(harnessLabel("codex")).toBe("Codex");
    expect(harnessLabel("dsh")).toBe("DeepSeek (DSH)");
  });

  it("shows an unknown kind raw rather than hiding it", () => {
    // A harness the UI hasn't learned about is still a fact about the agent;
    // rendering nothing would read as "no engine".
    expect(harnessLabel("acme-harness")).toBe("acme-harness");
  });

  it("covers every kind it claims to know", () => {
    for (const kind of KNOWN_HARNESS_KINDS) {
      expect(HARNESS_LABELS[kind]).toBeTruthy();
    }
  });
});

describe("default engine", () => {
  it("is the server's first entry", () => {
    // `/api/backends` puts the default kind first, so the server decides the
    // default and the client just reads it — one place to change it.
    expect(defaultHarnessKind(["dsh", "claude-code", "codex"])).toBe("dsh");
    expect(defaultHarnessKind(["claude-code", "codex"])).toBe("claude-code");
  });

  it("falls back to the known default before /api/backends answers", () => {
    expect(defaultHarnessKind([])).toBe(KNOWN_HARNESS_KINDS[0]);
  });
});

describe("engine choices", () => {
  it("offers what the server reports", () => {
    expect(harnessChoices(["dsh", "codex"])).toEqual(["dsh", "codex"]);
  });

  it("falls back to the known set rather than rendering an empty picker", () => {
    expect(harnessChoices([])).toEqual(KNOWN_HARNESS_KINDS);
  });
});
