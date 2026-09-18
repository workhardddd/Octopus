# Octopus

Octopus is a personal agent platform: durable agents reachable from a browser,
a phone or Telegram, each running on an external CLI harness.

## Language

### Core objects

**Agent**:
The durable definition of an assistant — persona, model, harness kind,
credential, tools, connectors. Agents own sessions, schedules and bridge
bindings.
_Avoid_: bot, assistant definition

**Session**:
One conversation thread with an agent: ordered messages, a working directory,
an origin, and an engine-side resume id.
_Avoid_: chat, thread, conversation (that last one means an app-agent thread)

**Turn**:
One user message and the settled response to it. A turn may contain many tool
calls and several model steps.
_Avoid_: request, exchange, round

**Working directory**:
The directory a session's turns run in. It is fixed when the session is created
and is the identity DSH binds a resumed session to.
_Avoid_: cwd, project dir, workspace (DSH uses "workspace" for its own concept)

**Origin**:
Why a session exists: `user`, `schedule`, `bridge`, `delegation`, `fork`,
`application`, `app`. It decides who is expected to answer a question.
_Avoid_: source, trigger

### Harness

**Harness layer**:
The single boundary through which Octopus talks to a model runtime — one
`Harness` front door and one `HarnessRun` engine, never per-framework classes.
_Avoid_: backend layer, adapter layer, CLI layer

**Harness kind**:
Which runtime a session runs on: `claude-code`, `codex` or `dsh`. The
persisted and wire field that holds it is `backend`.
_Avoid_: backend (when you mean the layer), engine, provider

**Runtime profile**:
The data record that describes one harness kind — binary, prompt blurb,
credential style, argv rendering, event parsing, capability flags.
_Avoid_: profile, adapter, driver

**DSH**:
The DeepSeek Harness CLI (`dsh`). Always the short form; never "DeepSeek
Harness", which collides with Octopus's own harness layer.
_Avoid_: DeepSeek Harness, the harness (unqualified)

**DSH profile**:
One of DSH's own composable plugin stacks, selected with `dsh --profile <name>`
— for Octopus, `acp` for turns and `headless` for one-shots.
_Avoid_: profile (unqualified — that is a runtime profile)

**DSH home**:
The `DSH_HOME` directory one DSH process reads and writes: its sessions,
settings, credentials, profiles and instruction file. Octopus keeps one per
agent.
_Avoid_: harness home, config dir

**ACP session**:
DSH's own conversation handle, created by `session/new` and continued by
`session/resume`. It is the resume id of a DSH session, not an Octopus session.
_Avoid_: session (unqualified)

**Run**:
One harness process together with the event stream it produces. A run serves
one turn, and may be held open to serve the next one.
_Avoid_: process, turn

**Held process**:
A run kept alive after its turn so the next turn skips the spawn cost and keeps
the prompt cache warm. Only harness kinds that take input on a live pipe have
them.
_Avoid_: warm process, pooled process

**Resume id**:
The engine-side handle that lets a later run continue the same conversation
(Claude session id, Codex thread id, ACP session id). The column is still named
`claude_session_id` for back-compat.
_Avoid_: session id (unqualified), thread

**One-shot**:
A lean, tool-free model call used by Octopus itself — schedule parsing, file
reference resolution, research synthesis.
_Avoid_: completion, single-shot

### Conversation control

**Prompt assembly**:
Composing a turn's system prompt and MCP set from the agent, its connectors and
the session's memory. Shared by every harness kind; only rendering differs.
_Avoid_: prompt building, context assembly

**Steering**:
Injecting a user message into the turn already running. A harness kind either
supports it or queues the message instead.
_Avoid_: interrupt (that ends the turn), injection

**Rewind**:
Branching a session to an earlier user message, replaying the truncated
history into the new branch's first turn.
_Avoid_: fork (that duplicates a whole session), rollback

**Fork**:
Duplicating a session onto its own copy of the working directory, so the copy
can diverge independently.
_Avoid_: clone, branch, rewind

**Sub-agent**:
A helper a model spawns inside one tool call: ephemeral, anonymous, scoped to
that turn.
_Avoid_: child session, delegation

**Delegation**:
A real session, under another agent, started by `mcp__ask_agent__ask` so that
agent keeps its own transcript.
_Avoid_: sub-agent, handoff

### Failure and limits

**Degradation**:
A capability a harness kind knowingly does not provide, declared on its profile
so the UI and the docs can say so, instead of falling back silently.
_Avoid_: limitation, gap, missing feature

**Stale session**:
A resume id the engine no longer recognises — its store was rotated, deleted
or moved. The recovery is to drop the id once and start a fresh conversation.
_Avoid_: expired session, lost session

**Re-auth required**:
A credential the engine rejected (expired, revoked). Retrying cannot fix it;
the credential must be replaced.
_Avoid_: auth error, login failure
