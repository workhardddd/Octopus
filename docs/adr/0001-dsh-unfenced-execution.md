# Run the DSH harness unfenced

DSH's filesystem sandbox has exactly one writable root per session — the
session's working directory plus platform temp — and no configuration can add a
second root (`dsh-sandbox-policy`). Octopus's agent memory is a **per-agent**
directory, while a DSH sandbox root is **per-session**: they can never coincide,
so under `workspace-write` an agent could not write its own memory. We therefore
run DSH with `sandbox-policy.mode: danger-full-access` and
`approval.policy: never`, which is the same posture the existing kinds already
run under (`claude --dangerously-skip-permissions`, `codex
--dangerously-bypass-approvals-and-sandbox`). DSH is not making Octopus less
safe; it is the first harness where a fence was even available.

## Considered options

- **`workspace-write` + per-call escalation** (`approval: ask`, Octopus
  auto-answering `session/request_permission`). Rejected: the ACP permission
  payload carries only a tool-call id, no path or command, so a path-based
  policy would have to be reconstructed from the update stream — and that
  bridge awaits its client with **no timeout and no abort signal**, so one
  unanswered request blocks a turn indefinitely.
- **`workspace-write` + memory written through an Octopus MCP tool.** The only
  option that keeps a real fence, since the writing process would be Octopus
  rather than a DSH child. Rejected *for now*: it changes what "the agent
  writes its memory" means, adds a server, and buys a boundary that no other
  Octopus harness has — so it would be safety theater in a system where the
  agent already has an unfenced shell through the other two kinds.

## Consequences

- DSH agents can read and write anywhere on the host, exactly like
  `claude-code`/`codex` agents today. There is no sandbox boundary to advertise.
- **Escalation is impossible**: with `policy: never`, a denied action is final
  and the model is told not to request escalation. Nothing depends on it today.
- If Octopus ever wants a real fence, the MCP-memory route above is the only
  one that keeps per-agent memory working — this ADR is the place to supersede.
