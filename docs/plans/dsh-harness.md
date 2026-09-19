# Tech Plan: DSH as a first-class harness kind

Status: **implemented** (2026-09-19) — all six phases landed on
`feature/dsh-harness` (8 commits). What the phases actually contained, where it
differed from the sketch above, and what is verified:

- **Phase 1-2** also carried the per-agent home and the generated patch
  (`server/dsh_home.py`), because a real DSH turn cannot run without them — the
  sketch put them in phase 4, and leaving them out would have made phase 2
  verifiable only against the fake CLI.
- The engine's protocol collaborator is `RuntimeProfile.new_protocol` (a
  *factory*, like `new_event_parser`), and `EventParser` grew `flush()` so a
  parser that coalesces ACP's `messageId` chunks can emit the message before the
  turn's terminal event.
- **Verified against the real CLI** (dsh 0.1.5-rc.2, this machine):
  `tests/test_dsh_profile_conformance.py` 4/4 — including the guard that the
  shipped rows still carry exactly the fields the patch restates — and
  `tests/test_backend_dsh_real.py` 7/7 (a real turn, a second turn on the held
  process, a resume across a fresh process, a `headless` one-shot, a rejected
  key classified as an auth error, a **research web leaf**, and a **dangling
  resume id**). `web/e2e/dsh.spec.ts` adds a mocked dialog test and a real turn
  through the UI (8.4s).
- **Two faults only the real CLI could find, both fixed** — they are why the
  last two real tests exist:
  - *The research web leaf could not boot.* Its patch disabled the sub-agent
    **service** rows (`subagent`, `subagent-spawn-in-process`,
    `subagent-fork-in-process`) alongside their tools, and the composed tree
    injects those rows, so `dsh` failed to start: the ACP handshake returned a
    bare `Internal error` with an empty stderr, which the leaf's never-raise
    contract turned into `LeafResult(error=…)`. Deep research on DSH was
    therefore dead end to end — and §3.9's conformance test accepted the patch
    the whole time, because `--dump-config` composes the tree without booting
    it. Bisecting the row list against the CLI settled it: sub-agent *tools*
    disable cleanly, *services* do not. The leaf now turns off tools only (§10
    row 10).
  - *The stale-session table was a guess.* `_DSH_STALE_SESSION_PATTERNS` decides
    whether a session whose DSH store is gone drops its resume id or fails
    forever. A real dangling id reports `Invalid params: session is not
    resumable: <id>`, which matched none of the four patterns written from the
    docs — the recovery would have been a silent no-op.
- **Verified as far as this box allows**: pytest 1118 passed / 42 skipped / 0
  failed (Linux container; the 42 are its absent CLIs and `DEEPSEEK_API_KEY`),
  vitest 200/200, `tsc --noEmit` clean, Playwright's `:fast` bucket 41/41, and
  the `@llm` DSH UI turn 1/1. The other 36 `@llm` tests drive `claude`/`codex`
  and are not runnable here for reasons that predate this branch (§8).
- **Two open items from §3.5, both resolved rather than deferred**: the
  `sandbox-policy.mode` vs `permission.defaultPreset` question (DSH *infers* a
  preset, so the patch names it explicitly — the conformance test would fail if
  that stopped being accepted), and the symlinked-profile / symlinked-`AGENTS.md`
  questions (both work where symlinks exist; where they do not, the view falls
  back to a copy refreshed each spawn, and the profile workspace falls back to
  DSH initializing its own).
- **Supersedes nothing**: `harness-layer.md` still describes the layer this adds
  a third kind to (with a dated note on the two generalizations it forced), and
  `codex-backend.md` still describes Codex.

## 0. Why this exists

The harness layer drives two external CLI agents — `claude` (Claude Code) and
`codex` — as `RuntimeProfile` values behind one `Harness` + one `HarnessRun`
(`harness-layer.md`). **DSH (`@deepseek-ai/dsh`, the DeepSeek Harness) is the
owner's primary harness**, so Octopus must drive it as a first-class kind: new
agents default to it, and it carries the same product surface (turns, streaming
events, resume, fork, memory, MCP-injected in-app tools, credentials).

This is **additive, not a replacement**. An earlier round of this review
considered deleting `claude-code`/`codex` outright; that was withdrawn. Both
kinds stay, with all their existing agents, sessions, credentials, login flows,
handoff/pull and per-harness quirks untouched. The cost of keeping them is that
the three-way asymmetry stays real — §10 records what each kind does *not* do,
so a missing feature is a declared degradation rather than a silent fallback.

### 0.1 The facts this design is built on

Everything below was verified against the installed `dsh` 0.1.5-rc.2
(`@deepseek-ai/dsh` + 239 bundled `@deepseek-ai/*` packages, READMEs, shipped
`lib/*.js`, `lib/types/*.d.ts`, and the live `$DSH_HOME`). The plan records the
load-bearing ones because they are *not* obvious from the outside:

1. **Two automation surfaces exist and neither is a JSONL event stream.**
   `dsh --profile acp` is a standard **ACP v1** stdio JSON-RPC server
   (`initialize`, `session/new|list|resume|close`, `session/set_config_option`,
   `session/prompt`, `session/cancel`, `session/update`,
   `session/request_permission`). `dsh --profile sdk` is a different, minimal
   7-message DSH-specific protocol. `dsh --profile headless "<task>"` answers one
   task and exits.
2. **ACP is the only surface that claims public stability**, and the only one
   with per-session `cwd` + per-session MCP declarations + cancellation +
   cross-process `session/resume`.
3. **No stable surface streams provider token deltas.** ACP carries only
   *committed* assistant messages/thoughts. Raw deltas exist on an in-process
   Cordis event and the product GUI's internal WebSocket — neither is a public
   contract. (The SDK's committed `assistant/message` embeds a timestamped
   delta-boundary stream, recoverable post-hoc; we are not using it — §2.)
4. **One `dsh` process = one composed profile + one permission posture + one
   `DSH_HOME`.** ACP's only per-session knobs are `cwd`, `mcpServers`, `model`
   and `reasoning_effort`; permission/sandbox is *not* among them.
5. **The sandbox has exactly one writable root per session** (the session
   `cwd`) plus platform temp, and **no config field adds a second root**. A
   per-agent memory directory outside the session cwd can therefore never be
   writable under `workspace-write`. See the ADR.
6. **`approval: ask` + ACP blocks indefinitely.** That bridge awaits the client
   with no timeout and no abort signal; `never` short-circuits before the
   approval waterfall, and a non-ACP `ask` with no answerer fails closed. So an
   ACP driver must never leave a permission request unanswered.
7. **A `--patch` row *replaces* the whole `config` object — no deep merge.**
   `--patch` is the top of the patch stack (bundle → profile → `$DSH_HOME`
   layer → `--patch`), so an Octopus patch always wins; but it must restate every
   field it wants to keep. Empty/comment-only patch files fail boot (`[]` is the
   empty form).
8. **`DSH_HOME` is the single isolation knob** — sessions, `settings.yaml`,
   `.credentials.yaml`, profiles, the user-global `AGENTS.md` and `skills/` all
   travel with it. It must be **exported** into the child environment (`.env`
   files reject `DSH_*`).
9. **MCP has no `mcpServers` config key** — it is one plugin row per server, or
   (on ACP) the `mcpServers` array on `session/new`/`session/resume`, which is
   **required** (pass `[]` for none) and **not re-sent on resume unless passed
   again**.
10. **Session logs are Zstd-framed JSONL, append-only, and DSH has no deletion
    API.** They accumulate under `$DSH_HOME/sessions/--<cwd>--/<id>/`.
11. **`session/resume` is cwd-bound and machine-bound**: it verifies an absolute
    `cwd` against the stored header (`session cwd does not match`), so a session
    store does not move between directories or machines.
12. **DSH ships no compatibility promise.** The SDK declares "pre-release
    stance, no compatibility promise"; only ACP's public spec is claimed stable.
    The `docs/config-catalog.md` every README points at as "the exhaustive
    source" is **not in the npm tarball** — `lib/types/*.d.ts` is the substitute.

## 1. Goals

1. **A third `RuntimeProfile` value (`DSH`)** in `server/harness/dsh.py`, with
   the capability set declared as data — no harness subclasses, no backend-kind
   branching outside `server/harness/` (the invariant `harness-layer.md` §1
   established).
2. **Turns run on ACP**; one-shots (`/schedule` parsing, `/showme` resolution,
   the research reasoning leaf) run on `dsh --profile headless`.
3. **DSH is the default kind for new agents and sessions.** `claude-code` and
   `codex` remain selectable, and every existing row keeps working unchanged.
4. **Declared degradations, not silent ones.** Every ACP limitation (§10) is
   visible in `SessionInfo` predicates, in the UI where it applies, and in the
   docs — a missing feature must be explainable without reading the profile.
5. **No new Python dependency**: a minimal in-repo ACP client (the repo's
   standing "CLIs, not an SDK" decision, `architecture.md`).
6. **Zero behavior change for the existing kinds**: their argv/parser snapshots
   and every existing test stay green as-is.
7. **Full suite parity** per CLAUDE.md: pytest, vitest, `tsc --noEmit`, and
   Playwright, all green, with new DSH coverage (fake CLI + real-CLI gated).

## 2. Non-goals

- **Not removing `claude-code`/`codex`**, and not touching their login flows,
  JSONL codec, fork strategies, `--agents` sub-agents, or quirks.
- **Not implementing token-level streaming for DSH.** ACP does not carry
  deltas; we accept committed-message granularity (§10). The three escape
  hatches (SDK post-hoc replay, a local Cordis plugin on
  `agent/assistant-stream`, the internal GUI WebSocket) are recorded in §11 as
  *rejected*, not deferred.
- **Not implementing inline steering for DSH.** ACP has no mid-turn input
  channel; `can_steer=False` and messages queue, exactly as Codex already does.
- **Not using `dsh --profile sdk`** (no cancel, no session close, process-level
  `cwd`) **or `dsh --profile sdk-minimal`** (its `DSH_SYSTEM_PROMPT` env
  injection is tempting, but the profile has one shell tool and no fs tools,
  skills, instructions, compaction or Harness identity).
- **Not planning a tools/skills product surface.** DSH mounts a rich tool set
  and `skill-filesystem` has `customSkillDirs`, but Octopus has no skills
  concept to wire; that is a product decision, not this plan's.
- **Not one process serving many sessions** (§3.6).
- **Not handoff/pull for DSH** — it is a Claude-JSONL product; the third kind
  has `transcript_codec=None`.
- **Not Windows** *(superseded 2026-09-19)*: `run.py`'s process-group reaping
  (`start_new_session`, `os.killpg`, `signal.SIGKILL`) was POSIX-only, which is
  why this plan left it alone. It has since been made platform-aware in
  [`windows-support.md`](windows-support.md) — and the import-time
  `signal.SIGKILL` that made the server unbootable on Windows went with it — so
  the DSH kind runs on either platform.

## 3. Design

### 3.0 The shape of the change

Three pieces, in dependency order:

1. **A protocol collaborator in the engine** (§3.1) — because ACP is not "one
   JSON event per line, prompt on argv or as a raw stdin frame". This is the
   only change to shared code and it must leave the two existing kinds
   byte-identical.
2. **`server/harness/dsh.py` + `server/harness/dsh_acp.py`** (§3.2–§3.4) — the
   profile value, the ACP client, the event mapping.
3. **Home / patch scaffolding, credentials, defaults, frontend** (§3.5–§3.9).

### 3.1 Engine: one new collaborator, no subclasses

`HarnessRun` today writes either nothing (stdin closed) or a raw user frame, and
treats every stdout line as an event. ACP needs four things it cannot express:
a spawn-time handshake, request/response correlation, server→client requests
that must be answered, and turn ownership of cancellation.

Rather than special-case the engine, the profile gains a **`TerminalProtocol`**
collaborator — the same "irreducible collaborator referenced by a profile"
precedent `EventParser` and `LoginDriver` already set (`harness-layer.md` §3):

```python
class TerminalProtocol(Protocol):
    stdin_mode: StdinMode

    async def handshake(self, run: HarnessRun, ctx: TurnContext) -> str | None:
        """Spawn-time protocol setup. Returns the engine-side conversation id
        to persist (ACP: initialize + session/new|resume), or None."""

    async def send(self, run: HarnessRun, text: str) -> str | None:
        """Deliver one user turn; returns a frame id for echo matching."""

    def classify(self, obj: dict) -> Frame:
        """event | response(id, payload) | server_request(id, method, params)"""

    async def answer(self, method: str, params: dict) -> dict | None:
        """Answer a server→client request. None = method not found (-32601)."""

    async def cancel(self, run: HarnessRun) -> None:
        """Turn-owned cancellation (ACP: session/cancel)."""
```

- `STREAM_JSON` and `CLOSE_AFTER_SPAWN` get a thin refactor of today's behavior
  (`send_user_frame` becomes Claude's `send`; `classify` returns `event` for
  everything; `handshake`/`answer` are no-ops). Their argv and parser snapshots
  must not move — the existing tests are the oracle.
- `StdinMode` gains a third member (the protocol owns the pipe; the engine does
  not write raw frames and does not close it).
- The engine keeps a **pending-request map** keyed by JSON-RPC id so a response
  frame can settle the call that is waiting on it. For DSH the turn's terminal
  condition is the **`session/prompt` response** (ACP settles it after agent
  idle and ordered update delivery), not an event: the engine emits `result`,
  persists the resume id, and closes the stream there.
- A response carrying a JSON-RPC error becomes an `error` event with the wire
  `code`/`message` preserved (this is what the auth/stale/transient predicates
  classify).

### 3.2 The `DSH` profile

```python
DSH = RuntimeProfile(
    backend="dsh",
    binary="dsh",
    tools_prompt=_OCTOPUS_SYSTEM_PROMPT_DSH,   # same built-ins, DSH wording
    credential_style="env_secret",             # DEEPSEEK_API_KEY
    premature_exit_recovery=False,             # a Claude-CLI quirk; DSH has none
    stdin_mode=StdinMode.PROTOCOL,
    protocol=DSH_ACP,                          # dsh_acp.py
    build_turn_argv=_build_turn_argv,          # dsh --profile acp --patch …
    build_oneshot_argv=_build_oneshot_argv,    # dsh --profile headless "<task>"
    parse_oneshot_stdout=str.strip,            # final answer is stdout
    auth_error_patterns=_DSH_AUTH_PATTERNS,
    transient_error_patterns=_DSH_TRANSIENT_PATTERNS,
    stale_session_patterns=_DSH_STALE_PATTERNS,  # "session cwd does not match", …
    web=WebCapability(("web_search", "web_fetch"), combined=False),
    injects_memory_prompt=False,               # native agent-instructions, §3.5
    can_fork=True,                             # /rewind via HISTORY_REPLAY
    fork_prepare=_fork_prepare_replay,         # shared with claude/codex
    fork_copy=None,                            # ⇒ /fork falls back to replay
    login=None,                                # API key, no login flow
    transcript_codec=None,                     # ⇒ can_export/can_import False
)
```

Declared degradations this encodes (all derived, none hand-maintained):
`can_steer=False` (not `STREAM_JSON`), `can_export=False`, `can_import=False`,
`subagents` inert (no `--agents` analogue — the profile simply does not render
them), tool allow/deny inert (§10.6).

### 3.3 Turn argv, handshake, and the ACP client

**argv**: `dsh --profile acp --patch <octopus patch>` (+ `--patch <research
patch>` for a web leaf). The profile is `startup`-reload, so a patch edit needs
a fresh process — which is exactly Octopus's model (`spawn_signature` already
forces a respawn when anything baked in at spawn moves).

**`server/harness/dsh_acp.py`** implements a minimal ACP client over the same
subprocess pipes: newline-delimited JSON-RPC 2.0, a pending-request map, and
these calls only — `initialize`, `session/new`, `session/resume`,
`session/prompt`, `session/cancel`, plus the `session/update` and
`session/request_permission` inbound sides. Two wire quirks are implementation
rules, not surprises:

- `session/new`'s `mcpServers` is **required** — pass the assembled list, or
  `[]`. On `session/resume` it must be passed **again**; omitting it means no
  MCP at all.
- `session/set_config_option` takes a **string** `value`, and the `model`
  option's value is an opaque `JSON.stringify([provider, model])` pair. The
  driver parses and emits that pair; `agent.model` is the model id with
  provider defaulting to `deepseek-official`, and a value containing `/` splits
  as `provider/model`.

**Permission requests are answered, always.** Under our patch (`policy: never`)
`approval/request` never fires, so `session/request_permission` should never
arrive — but the bridge has no timeout, so the driver answers every request it
does receive (reject-once by default) rather than risking a turn that sits
forever. This is defence, not policy.

**Cancellation**: `interrupt()` sends `session/cancel` and then stops the
process (the existing escalation ladder is unchanged).

### 3.4 Event mapping

| ACP | `HarnessEvent` | Notes |
|---|---|---|
| `session/new` / `session/resume` result | `session_started` (`session_id`) | the ACP session id **is** DSH's `SessionHeader.id` |
| `session/update` → committed assistant message chunk | `text` | `text_delta` is never emitted (§10.1) |
| `session/update` → thought chunk | `thinking` | |
| `session/update` → tool call (pending/in-progress/completed) | `tool_use`, then `tool_result` | A DSH sub-agent arrives as an ordinary tool call pair (§10.3) |
| `session/prompt` response | `result` (+ `session_id`) | turn settlement; `cost` stays `None` (ACP reports context usage, not USD) |
| JSON-RPC error frame | `error` | feeds the auth/stale/transient predicates |
| — | `question_request` | unchanged: it comes from **our** `ask` MCP server, not the CLI |
| — | `subagent` | never emitted on ACP |

### 3.5 Home, patch, and memory

**Per-agent `DSH_HOME`** (`<dsh_home_dir>/agents/<agent_id>/`) so Octopus's
sessions, credentials and instructions never mix with the owner's own `dsh`
usage on the same box. Two provisioning consequences, both handled here:

- **Profile initialization is per-home.** The first boot materializes a plugin
  workspace inside `$DSH_HOME/profiles/<name>` (this machine's
  `~/.dsh/profiles/web` holds `node_modules/` + `pnpm-lock.yaml`). Doing that
  N times is unacceptable, so Octopus **provisions one shared profile workspace
  once** (`<dsh_home_dir>/profiles/`, warmed for both `acp` and `headless`) and
  **symlinks it into every agent home**. *Verification item*: confirm DSH
  follows a symlinked `profiles` directory; if it does not, fall back to a
  single Octopus-owned home plus patch-pointed per-agent instruction/skill
  paths, which is the only reason the per-agent home exists.
- **Profile init can outlive a turn budget.** The first spawn in a cold home may
  take far longer than a normal turn, while `turn_idle_timeout_seconds` keeps
  ticking. Provisioning is therefore explicit and happens at **agent create**
  (and at Octopus boot for existing agents), not lazily inside a turn, and a
  failure is surfaced as its own error rather than as a turn timeout.

**The patch is a pure function of the turn.** `<agent_home>/patches/<spawn
signature>.yml` is regenerated per spawn from the same inputs
`spawn_signature` already hashes (persona, model, MCP set, connectors, memory
dir, `web_research`), and stale patch files are purged on spawn:

```yaml
# Octopus-generated — regenerate, do not hand-edit.
- id: system-prompt
  config:
    personaPrefix: |-
      <persona + Octopus in-app-tools blurb + connectors blurb>
    personaSuffix: Your working directory is {{cwd}}.
- id: agent-instructions
  config:
    dshHome: <agent memory dir>      # native read path for agent memory
    maxBytes: 262144
- id: approval
  config:
    policy: never                    # never prompt; escalation impossible
- id: sandbox-policy
  config:
    mode: danger-full-access         # posture (A) — see the ADR
```

*Verification item*: whether the session's initial sandbox mode is taken from
`sandbox-policy.mode` or from `permission.defaultPreset`. If the latter wins,
this patch must additionally restate the whole three-entry `presets` table
(patches do not deep-merge) and set `defaultPreset`.

**Memory keeps one source of truth.** The canonical per-agent directory
(`agent_memory.agent_memory_dir`) is unchanged and still shared by all three
kinds, so "memory lives on the agent, survives an engine swap" stays true.
DSH's native read path points at that directory (`agent-instructions.dshHome`),
with `AGENTS.md` as the file DSH loads; Octopus provisions it (a symlink to the
canonical `MEMORY.md` if DSH follows symlinks — verified on the real CLI — else
a generated copy kept in sync on write). Because the posture is unfenced, the
agent's own file tools can write it directly, which is why this plan does not
need a memory MCP server.

**Credentials are injected, never stored in the DSH home.** The agent's
credential is decrypted as usual and passed as `DEEPSEEK_API_KEY` in the child
environment (the launch environment outranks every credentials file), so N
agent homes do not mean N copies of the key at rest.

**Session-store cleanup.** DSH never deletes session logs. A hard session
delete in Octopus therefore also removes `<agent_home>/sessions/*/<resume
id>/`, best-effort and never failing the delete; archiving deletes nothing
(same semantics as every other kind).

### 3.6 Process and session model

One Octopus session = one ACP session = one `dsh` process, reusing today's
held-process machinery (`spawn_signature` for reuse eligibility, `send_turn`
for a follow-up turn, the idle reaper and LRU cap unchanged). `run.py`'s
one-process-one-stream unit is preserved deliberately: the permission posture
and the `DSH_HOME` are **process**-level while the working directory is
**session**-level, so a process per session is the honest mapping. ACP's
multi-session-per-connection capability is not used.

`start(resume_id)`: `session/resume` when the session already has a DSH id
(re-sending `mcpServers`), else `session/new`. `send_turn`: `session/prompt`.
A dead store or a moved working directory surfaces as a stale-session error and
the existing once-per-turn recovery drops the id and starts fresh.

### 3.7 One-shots

`run_oneshot` renders `dsh --profile headless "<task>"`, using the owning
agent's home when there is one and a shared `<dsh_home_dir>/_oneshot/`
otherwise. stdout is the final answer; a non-zero exit maps to
`HarnessOneshotError("failed")`. This is what makes `/schedule` parsing,
`/showme` resolution and the research reasoning leaf work on DSH — all three
call `harness.run_oneshot` already and need no change.

The research **web leaf** is a full `HarnessRun`, so it needs a *scoped* patch:
the leaf renders `web_research=True`, and the DSH profile turns that into a
second patch overlay that disables the destructive/fan-out tool rows
(`tool-bash`, `tool-pwsh`, `tool-fs`, `tool-fs-search`, `tool-subagent`,
`tool-workflow`, …) while keeping `tool-web` and the web-search/fetch rows,
plus `mcpServers: []`. Tool policy cannot be set per turn on ACP, so a second
spawn profile is the only mechanism — this is a design consequence, not a
shortcut.

### 3.8 Availability, defaults, credentials, frontend

- **Availability**: the registry registers `DSH`; `is_available()` resolves
  `dsh` through the existing `_which_with_fallback` (nvm/user bin dirs
  included). `DEFAULT_BACKEND` becomes `"dsh"`, and `main.py` force-lists the
  default kind in `/api/backends` (today it force-lists `claude-code`).
- **Defaults**: every Python-side default (`agent_manager.create_agent`,
  `database.create_agent`/`save_session` signatures, `session_manager.
  create_session`, the `or "claude-code"` fallbacks in `routers/*`,
  `app_agent.py`, `applications.py`, `delegations.py`) reads one exported
  constant instead of a literal. **The SQL column defaults stay
  `'claude-code'`** — they are a safety net, every INSERT passes a value, and
  changing them would mean a migration for no behavior.
- **Credentials**: `HarnessPage` gains a third, much simpler form (paste an API
  key). `routers/credentials.py` needs one branch for "this harness has no
  login driver" — the two hardcoded flows (`oauth/*`, `/codex/*`) are untouched
  and still reachable for their own kinds.
- **Frontend**: `contracts.ts` is regenerated (`bun run generate:contracts`),
  never hand-edited. `AgentFormPage`'s engine list and labels stop being the
  hardcoded `["claude-code","codex"]` literal and become data-driven over
  `/api/backends` with a label map; `SidebarAgents`' create-session select and
  `sessionStore`'s initial `availableBackends` follow the new default. The
  model field hints the `provider/model` form for DSH.

### 3.9 The conformance test (the one that protects the whole design)

Our patch depends on shipped field names and the shape of the composed tree.
`dsh --profile acp --dump-config` prints that tree. A gated test asserts the
rows and fields this plan relies on are still present with compatible
semantics (`system-prompt` schema fields, `approval.policy`,
`sandbox-policy.mode`, `agent-instructions.dshHome`/`maxBytes`, the tool rows
the research overlay disables, `agent-default-model`). A DSH upgrade that
renames or drops one of them then fails **loudly in CI**, instead of silently
running agents with a dropped persona or an unpinned posture. Given §0.1.12
(no compatibility promise, no shipped config catalog), this test is the
difference between "tracks the CLI" and "breaks quietly".

**What it cannot catch**, learned the hard way: `--dump-config` *composes* the
tree, it does not *boot* it. A patch that disables a row the composed tree
injects — the leaf's sub-agent services were exactly that (§10 row 10) — prints
a perfectly good tree and then fails at startup, with nothing on stderr. Field
names and row shapes are what this test protects; **that a turn actually starts
and answers is only provable by running one**, which is why
`test_backend_dsh_real.py` runs the leaf and the dangling-resume recovery for
real rather than asserting their argv.

## 4. Files

**New — server**

| File | Purpose |
|---|---|
| `server/harness/dsh.py` | The `DSH` profile: argv, one-shot, patch rendering, error-pattern tables, registration. |
| `server/harness/dsh_acp.py` | Minimal ACP client + the `TerminalProtocol` implementation. |
| `server/dsh_home.py` | Per-agent home provisioning (dirs, shared `profiles` symlink, patch files, `AGENTS.md` view, session-store purge). |

**Modified — server**

| File | Change |
|---|---|
| `server/harness/profile.py` | `TerminalProtocol` field + `StdinMode.PROTOCOL`. |
| `server/harness/run.py` | Delegate handshake/send/classify/answer/cancel; pending-request map. |
| `server/harness/registry.py` | Register `DSH`; `DEFAULT_BACKEND = "dsh"`. |
| `server/models.py` | `BackendKind.dsh`; model-field doc for the provider/model form. |
| `server/session_manager.py` | One shared default-kind constant; session-store purge on hard delete. |
| `server/database.py` | Python-side defaults only (SQL defaults unchanged). |
| `server/main.py` | Force-list the default kind in `/api/backends`. |
| `server/config.py` | `dsh_home_dir`. |
| `server/harness/harness.py` | Thread `agent_id` / `dsh_home` / `dsh_patch` into the turn and one-shot contexts. |
| `server/routers/credentials.py` | "No login driver" branch. |
| `server/routers/{agents,sessions}.py`, `server/agent_manager.py` | Default kind via the shared constant on the create paths; read-side `or "claude-code"` fallbacks and `import_session` deliberately untouched. |
| `server/showme_ai.py`, `server/schedule_ai.py`, `server/research/{leaf,orchestrator,manager}.py`, `server/routers/files.py` | Pass `agent_id` through, so a one-shot or a leaf runs with the right DSH home. |

**New — frontend**: `web/src/lib/harness.ts` (+ `harness.test.ts`) — the one
place that knows a kind's display name, how to show an unknown kind, and which
kind the server calls the default. `web/e2e/dsh.spec.ts` — the mocked credential
dialog plus a real `@llm` turn.

**Modified — frontend**: `web/src/api/contracts.ts` (regenerated),
`components/HarnessPage.tsx` (third credential form),
`components/AgentFormPage.tsx` + `components/SidebarAgents.tsx` (data-driven
engine list, labels, defaults, and the note that a DSH agent's tool policy is
inert), `components/QuestionPrompt.tsx`, `stores/sessionStore.ts` (initial
availability + default), `hooks/useWebSocket.ts` (default-kind literal).
`CredentialPicker.tsx` needed no change — its filter was already
backend-agnostic. Two existing specs (`new-features`, `agent-collaboration`)
now pin `claude-code` explicitly, because they assert claude-shaped behaviour
and the default kind moved.

**Docs**: this plan; `docs/adr/0001-dsh-unfenced-execution.md`; `CONTEXT.md`;
`docs/architecture.md`; `README.md`; `CLAUDE.md` (structure, test table,
real-CLI gate list); `docs/backup-and-migrate.md` (new state paths);
`docs/plans/harness-layer.md` (status: three kinds now); `docs/README.md`
(index).

## 5. Phased implementation (each phase leaves the suite green)

1. **Engine.** `TerminalProtocol` + `StdinMode.PROTOCOL` + the pending-request
   map; the two existing profiles move onto it mechanically. Oracle: every
   existing argv/parser/stream test unchanged, plus new engine tests driven by
   a fake protocol.
2. **ACP client + profile.** `dsh_acp.py`, `dsh.py`, a scripted fake ACP server
   fixture, and the event-mapping tests (handshake, required `mcpServers`,
   `set_config_option` pair, cancel, answer-every-request, error frames).
3. **One-shots.** headless argv/stdout + `HarnessOneshotError` mapping, and the
   `/schedule` + `/showme` + research-reasoning paths exercised on DSH.
4. **Home, patch, memory, cleanup.** `dsh_home.py`, patch rendering, the
   `AGENTS.md` view, the session-store purge, and the cold-home provisioning
   failure path.
5. **Defaults, credentials, frontend.** `DEFAULT_BACKEND`, `/api/backends`,
   contracts regeneration, the engine list/labels/defaults, the credential
   form, and the credentials-router branch.
6. **Tests and docs.** New fake + real-CLI + conformance tests, the three
   suites' counts re-derived, CLAUDE.md/architecture/backup docs updated.

## 6. Tests

**New**

- `tests/test_harness_dsh.py` — 15 cases: argv snapshots (incl. patch path and
  the research overlay), home/credential env, the refusal without a home or a
  patch, event mapping, `can_*` predicates, error-pattern classification,
  one-shot argv/stdout, fork strategy (`fork_copy is None` ⇒ replay), and the
  ACP client itself against a scripted fake process — required `mcpServers`,
  resume re-sending MCP, `session/cancel`, answering
  `session/request_permission`, a JSON-RPC error → `error` event,
  response/settlement ordering, `messageId` chunk coalescing, malformed-line
  tolerance.
- `tests/test_dsh_home.py` — home layout, shared-profile symlink, patch
  rendering + purge, `AGENTS.md` provisioning, credential-by-env, session-store
  purge on delete (and that it never fails the delete).
- `tests/test_dsh_profile_conformance.py` — the §3.9 `--dump-config` assertions
  (gated on `dsh` on PATH; a missing row is a **failure**, never a skip).
- `tests/test_backend_dsh_real.py` — 7 real turns: text + settle, the held
  process serving the next turn, `session/resume` in a fresh process, a
  `headless` one-shot, a rejected key classified as an auth error, a **web leaf
  running scoped**, and a **dangling resume id classified as stale** (the last
  two are the regression tests for the faults in the status header); gated on
  `dsh` on PATH **and** a usable DeepSeek credential, with the existing
  probe-timeout-is-an-error rule (`tests/cli_gate.py`).
- `tests/_fixtures/fake_dsh_acp.py` — the scripted fake CLI fixture.
- Frontend: `HarnessPage`/`AgentFormPage` cases for the DSH credential form and
  the data-driven engine list; `web/e2e/dsh.spec.ts` (an `@llm` spec mirroring
  `codex.spec.ts`: pick the DSH engine, send a prompt, assert the reply),
  skipped-when-absent exactly like `codex.spec.ts`.

**Extended**

- `tests/test_harness_core.py` — the protocol collaborator contract.
- `tests/cli_gate.py` — a `dsh_cli_works()` probe.
- `tests/test_credentials_api.py` — the no-login-driver branch.
- `tests/test_api.py` / `test_agents_api.py` — `/api/backends` now lists the
  default kind, and agent create defaults to `dsh`.

**Deliberately unchanged** (the anti-regression oracle for goal 6): the Claude
and Codex argv/parser snapshots, `test_jsonl_*`, `test_cli.py`,
`test_oauth_login.py`, `test_codex_login*.py`, `test_fork_native_copy*.py`,
and the real-CLI Claude/Codex suites.

## 7. Risks

1. **DSH has no compatibility promise (0.1.5-rc.2).** *Mitigation*: the §3.9
   `--dump-config` conformance test; all DSH coupling lives in three files.
2. **Cold-home profile initialization is slow and needs the network.**
   *Mitigation*: explicit provisioning at agent create/boot, one shared
   pre-warmed profile workspace, its own error path, and a symlink fallback
   (§3.5).
3. **A permission request with no answer hangs a turn.** *Mitigation*: patch
   `approval.policy: never` (the bridge never fires) **and** answer every
   request we do receive.
4. **`session/resume` is cwd-bound.** A moved working directory loses the
   model-side context. *Mitigation*: classified as a stale session and
   recovered once per turn; Octopus's own transcript is unaffected.
5. **Accepted degradations look like bugs later.** *Mitigation*: §10 is the
   single list; `can_steer`/`can_export`/`can_import` are surfaced in
   `SessionInfo`, and the docs say so.
6. **Three kinds, three asymmetries.** Keeping `claude-code`/`codex` means the
   matrix stays. *Mitigation*: every gap is a declared profile property, and
   the engine has no kind branching outside `server/harness/`.
7. **Session logs accumulate forever** (no DSH deletion API). *Mitigation*: the
   purge on hard delete; §11 records that DSH-side retention is otherwise out
   of Octopus's hands.
8. **`dsh` resolves to a `.cmd`/`.ps1` shim on Windows.** Untestable here and
   irrelevant under the Linux/macOS target; recorded so nobody debugs it twice.

## 8. Acceptance criteria

- A box with `dsh` on PATH lists `dsh` in `GET /api/backends`, and a **new**
  agent defaults to it; existing `claude-code`/`codex` rows are untouched.
- A turn on a DSH agent streams `text` and `tool_use`/`tool_result` events,
  persists, broadcasts, and resumes after the process dies; `interrupt()`
  cancels via `session/cancel`.
- Declared degradations hold: `can_steer=False`, `can_export=False`,
  `can_import=False`, no `subagent` events, tool allow/deny inert, memory works
  through the canonical directory, `/rewind` and `/fork` succeed via replay.
- Zero behavior change for `claude-code`/`codex`: their snapshot tests and the
  whole existing suite pass unmodified.
- No new Python dependency; all DSH coupling lives in `server/harness/dsh*.py`
  and `server/dsh_home.py`.
- pytest / vitest / `tsc --noEmit` / Playwright all green, with CLAUDE.md's
  counts and real-CLI gate list re-derived. How far that reached on the dev
  machine (Windows, `dsh` installed and keyed): pytest is authoritative in a
  Linux container — 1126 passed / 42 skipped / 0 failed of 1168 — vitest
  200/200, `tsc --noEmit` clean, Playwright's `:fast` bucket 41/41, and the DSH
  `@llm` turn green. The other 36 `@llm` tests drive `claude`/`codex` and need
  both signed in. The process-group blocker that used to be part of that answer
  was fixed afterwards ([`windows-support.md`](windows-support.md)); what is
  left is that `claude` streaming collects zero events on Windows — reproduced
  identically from an untouched worktree of the parent commit, so it is not this
  branch's — and that this checkout has no `.venv`, which the e2e config starts
  the backend from.

## 9. Decisions taken (and where they live)

| Decision | Record |
|---|---|
| DSH is added as a third kind and is the default for new agents; `claude-code`/`codex` stay | §1.3, §3.8 |
| Turns on ACP, one-shots on `headless` | §3.3, §3.7 |
| Unfenced execution (`danger-full-access` + `approval: never`) | **`docs/adr/0001-dsh-unfenced-execution.md`** |
| Per-agent `DSH_HOME`, key injected by env, shared pre-warmed profiles | §3.5 |
| One ACP session = one Octopus session = one process | §3.6 |
| Hand-rolled ACP client, no new dependency | §1.5, §3.3 |
| Memory stays in the canonical per-agent dir, DSH reads it natively | §3.5 |
| Terminology (`harness layer`, `DSH`, `DSH profile`, `backend`) | **`CONTEXT.md`** |

## 10. Declared degradations (the DSH column)

| Capability | On DSH | Why |
|---|---|---|
| 1. Token-level streaming | none — committed messages only | ACP excludes raw deltas; no stable surface has them |
| 2. Inline steering | none — messages queue (`can_steer=False`) | ACP has no mid-turn input channel |
| 3. Native sub-agent card | degrades to a plain tool call pair | ACP carries generic tool lifecycle, not sub-agent events |
| 4. `/rewind` | works (HISTORY_REPLAY) | shared with the other kinds |
| 5. `/fork` | works via history replay, no native copy | `session/resume` is cwd-bound, so a fork on a new directory cannot resume natively |
| 6. Tool allow/deny | **inert** | ACP cannot set a per-turn tool policy; DSH's tools are profile-level (a scoped turn requires a second spawn profile) |
| 7. `--agents` sub-agents | inert (not rendered) | no DSH analogue; the profile simply does not render them |
| 8. handoff/pull | unsupported (`can_export`/`can_import` False) | Claude-JSONL product; DSH's own format is Zstd-framed and cwd-bound |
| 9. Sandbox | none (unfenced) | ADR 0001 |
| 10. Web leaf | works, via a second restricted spawn profile | tool policy is not per-turn; the patch may only disable tool rows — disabling an injected **service** row keeps `dsh` from booting at all, with a bare `Internal error` and empty stderr |

## 11. What this defers

Genuine deferrals only — work that needs a real second use case, an external
dependency, or a user decision:

- **One process serving many sessions** (§3.6) — needs a richer engine than one
  run per process; nothing today requires it.
- **Non-DeepSeek models through `dsh-llm-pi-ai`** — DSH-native, configured on
  the DSH side; Octopus neither promises nor blocks it.
- **Token-level streaming via a local Cordis plugin** on
  `agent/assistant-stream` — viable (a `--patch` can insert a local plugin
  without publishing one), but it buys an animation, not information.
- **A tools/skills product surface** — `skill-filesystem.customSkillDirs`
  works; Octopus has no skills concept to attach it to.
- **DSH store retention/compaction** — DSH owns that lifecycle; Octopus only
  purges on delete.
