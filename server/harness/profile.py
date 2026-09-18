"""The `RuntimeProfile` — one data record per harness kind.

This is the heart of the data-driven design (VM0's `Record<framework,…>`
shape in Python): there are no `ClaudeCodeHarness`/`CodexHarness`
subclasses. There is one `Harness` class, one `HarnessRun` engine, and
two `RuntimeProfile` *values* (`CLAUDE_CODE` in claude_code.py, `CODEX` in
codex.py) that supply the few genuinely per-framework pieces — argv
rendering, event parsing, one-shot, login, transcript codec — as data +
small collaborators.

The shared `assembly.py` pre-computes the neutral inputs (selected MCP
servers, composed system prompt) into a `TurnContext`; the profile's
`build_turn_argv` only renders that into a concrete CLI command.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol

from .events import HarnessCredential, HarnessEvent
from .login import LoginDriver


@dataclass
class McpServerEntry:
    """Backend-neutral MCP server spec (the connector `mcp_entry` shape).

    Built once by `assembly.select_mcp_servers`; each profile renders the
    list into its own config form (`--mcp-config` JSON for Claude,
    `-c mcp_servers.*` TOML for Codex)."""

    key: str
    command: str
    args: list[str]
    env: dict[str, str]


@dataclass(frozen=True)
class WebCapability:
    """How a backend does web search/fetch, for native deep research
    (native-deep-research.md §4). A profile with `web = None` has no web tools,
    so deep research is unavailable on it (gated like `can_fork`).

    `tool_names` are the backend's native web tool identifiers (Claude:
    WebSearch/WebFetch; Codex: web_search) — used to allow them on a research
    leaf and to recognize web activity in the event stream. `combined` marks a
    single search-and-read tool (Codex) vs separate search + fetch (Claude)."""

    tool_names: tuple[str, ...]
    combined: bool = False


@dataclass
class TurnContext:
    """Fully-assembled, neutral inputs for one turn — what `build_turn_argv`
    renders. The shared work (MCP selection, system-prompt composition,
    working-dir absolutization) already happened in `assembly.py`."""

    prompt: str
    working_dir: str                  # absolute
    resume_id: str | None
    system_prompt: str                # composed: persona + tools blurb + connectors
    model: str | None
    tool_allow: list[str] | None
    tool_deny: list[str] | None
    mcp_servers: list[McpServerEntry]  # selected built-ins + connectors
    credential: HarnessCredential | None
    # Per-agent native memory (docs/plans/memory.md): the canonical markdown
    # dir both harnesses point at. None when there's no owning agent.
    memory_dir: str | None = None
    # Native-deep-research web leaf (native-deep-research.md §4): when True, the
    # profile renders a SCOPED, read-only-ish turn that enables the backend's
    # web tools and forbids destructive/fan-out tools (no Bash/Write/subagents),
    # so a throwaway research leaf can search the web but can't touch the box.
    web_research: bool = False
    # Sub-agent definitions to register for this turn (native-subagents.md §6).
    # A profile without a surface for them simply doesn't render them.
    subagents: list[dict[str, Any]] = field(default_factory=list)
    # Per-agent DSH home and the generated patch file for this turn
    # (dsh-harness.md §3.5). Both None on every other harness kind, and the
    # DSH profile refuses to render a turn without them: a DSH process with no
    # explicit DSH_HOME writes its sessions into the user's own `~/.dsh`,
    # which is exactly what the per-agent home exists to prevent.
    dsh_home: str | None = None
    dsh_patch: str | None = None
    # The owning agent, when there is one. Neutral — every kind may see it, and
    # it is what a profile derives its own per-agent paths from.
    agent_id: str | None = None


@dataclass
class OneShotContext:
    """Inputs for a lean, tool-free single model call (`run_oneshot`)."""

    prompt: str
    model: str | None = None
    credential: HarnessCredential | None = None
    working_dir: str | None = None
    # DSH's per-agent home (dsh-harness.md §3.5). Required for a DSH one-shot:
    # without it the process would read and write the user's own `~/.dsh`.
    dsh_home: str | None = None
    # The owning agent, when there is one (see `TurnContext.agent_id`).
    agent_id: str | None = None


@dataclass
class ParseOutput:
    """Result of feeding one stdout JSON object to an `EventParser`."""

    events: list[HarnessEvent] = field(default_factory=list)
    end_of_stream: bool = False


class EventParser(ABC):
    """Per-turn stdout normalizer. A fresh instance is created for each run
    (`profile.new_event_parser()`), so it may hold the small per-turn state
    the protocols need — e.g. the captured session/thread id surfaced on
    `session_started` before `result` arrives."""

    @abstractmethod
    def parse(self, obj: dict[str, Any]) -> ParseOutput:
        """Map one parsed stdout JSON object to zero+ events, flagging
        end-of-stream when the turn's terminal event (result) lands."""

    def flush(self) -> ParseOutput:
        """Whatever this parser is still holding, as a final parse result.

        The engine asks for this just before a turn's terminal event, so a
        parser that coalesces pieces into one logical event (ACP delivers one
        assistant message as several `messageId`-tagged chunks) can emit it
        *before* the result rather than after the stream has closed. Parsers
        that emit as they parse have nothing to flush.
        """
        return ParseOutput()


class FrameKind(str, Enum):
    """What one stdout frame is, for a protocol that multiplexes kinds.

    A `PROTOCOL` run's stdout carries three different things — events the
    model runtime is reporting, responses to requests we sent, and requests
    the runtime is making of us — and they must be routed before anything
    tries to interpret them as events.
    """

    EVENT = "event"
    RESPONSE = "response"
    REQUEST = "request"


class TerminalProtocol(ABC):
    """How a harness's stdio is driven beyond "one event per line".

    `HarnessRun` owns the pipe, the writes and the response correlation; the
    protocol owns the *conversation*: what to send before the first turn, how
    to deliver a turn, which frames are events, and what to answer when the
    runtime asks us something. Only a `StdinMode.PROTOCOL` profile supplies
    one — the two frame-on-stdin modes keep rendering their own raw frames and
    treat every line as an event.

    Methods take the `HarnessRun` rather than the protocol holding it, because
    the run is rebuilt per turn while the protocol is a profile-level value
    (the same shape `EventParser`/`LoginDriver` already have). `Any` avoids a
    circular import: `run.py` imports this module.
    """

    async def handshake(self, run: Any, ctx: TurnContext) -> str | None:
        """Set the process up before the first turn is delivered (create or
        resume the engine-side conversation, apply per-session options).
        Returns the engine-side conversation id to persist, or None."""
        return None

    async def send_turn(self, run: Any, text: str) -> str | None:
        """Deliver one user turn. Returns a frame id the caller may match
        against an echo, or None."""
        raise NotImplementedError

    def classify(self, obj: dict[str, Any]) -> FrameKind:
        """What one parsed stdout object is."""
        return FrameKind.EVENT

    async def on_response(
        self,
        run: Any,
        request_id: Any,
        result: Any,
        error: Any,
    ) -> ParseOutput:
        """A response to a request this protocol sent. Returns the events it
        implies, including whether the turn (and so the stream) ends here."""
        return ParseOutput()

    async def on_request(
        self,
        run: Any,
        request_id: Any,
        method: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        """The complete response frame to write back for a runtime→client
        request. The protocol owns the error shape too, so a method it does
        not implement answers in its own protocol's way."""
        raise NotImplementedError

    async def cancel(self, run: Any) -> None:
        """Turn-owned cancellation. Default: stop the process."""
        await run.stop()


class TranscriptCodec(Protocol):
    """Read/write a harness's on-disk transcript format (export/import).

    Present only for harnesses that support handoff/pull (Claude's JSONL);
    `None` on a profile means export/import is unsupported (Codex)."""

    def parse_file(self, path: str) -> Any: ...
    def write_file(
        self,
        path: str,
        messages: list[Any],
        session_id: str | None,
        working_dir: str | None,
    ) -> None: ...


class StdinMode(str, Enum):
    """What a backend's stdin is for (inline-steering.md §6).

    One enum rather than two booleans: "the prompt arrives on stdin" and
    "stdin is closed right after spawn" are mutually exclusive, and separate
    flags could express the combination that must never exist.
    """

    #: Prompt is in argv; stdin gets EOF immediately. Codex reads stdin even
    #: with a positional prompt and blocks forever waiting on EOF, so closing
    #: it is what lets the turn proceed.
    CLOSE_AFTER_SPAWN = "close_after_spawn"

    #: Prompt — and any mid-turn follow-up — are written as JSON lines on
    #: stdin, which therefore stays open for the life of the process.
    STREAM_JSON = "stream_json"

    #: The pipe carries a request/response conversation, not raw prompts: the
    #: run's `TerminalProtocol` sets the process up at spawn, delivers each
    #: turn, and answers anything the CLI asks back. stdout may therefore
    #: carry responses and server→client requests alongside events, which is
    #: why `HarnessRun` routes frames instead of parsing every line as one.
    #: (DSH over ACP — docs/plans/dsh-harness.md §3.1.)
    PROTOCOL = "protocol"


@dataclass(frozen=True)
class RuntimeProfile:
    """Everything that differs between harness kinds, as one record."""

    backend: str                 # "claude-code" | "codex" (matches the persisted field)
    binary: str                  # "claude" | "codex"
    tools_prompt: str            # in-app-tools blurb (per-framework wording)
    credential_style: str        # "env_secret" | "home_dir"
    # Internal Claude-CLI bug workaround flag (not a product capability):
    # the session_manager run loop respawns with "continue" after a
    # premature mid-turn exit only when this is set.
    premature_exit_recovery: bool
    # How the CLI is fed its prompt, and therefore what stdin is for
    # (inline-steering.md §6).
    stdin_mode: StdinMode
    # Renderers / parsers (module functions in the profile's file):
    build_turn_argv: Callable[[TurnContext], tuple[list[str], dict[str, Any]]]
    new_event_parser: Callable[[], EventParser]
    build_oneshot_argv: Callable[[OneShotContext], tuple[list[str], dict[str, Any]]]
    parse_oneshot_stdout: Callable[[str], str]
    # The conversation driver for `StdinMode.PROTOCOL` (below the required
    # renderers because a dataclass may not put a default before them). None
    # for the two frame-on-stdin modes, whose frames the engine renders itself.
    #
    # A *factory*, like `new_event_parser` and for the same reason: a profile
    # is one frozen value shared by every run of its kind, while a protocol
    # holds per-run state (the turn in flight, the request it awaits). One
    # instance per run is what keeps two concurrent runs of the same harness
    # from answering each other's frames.
    new_protocol: Callable[[], TerminalProtocol] | None = None
    #: Per-spawn preparation that *does* touch the filesystem, given the fully
    #: assembled context and free to fill in whatever the profile needs there
    #: (DSH writes the agent's home, its generated patch and its memory view).
    #: Deliberately NOT part of the argv rendering path: `build_argv` promises
    #: to be side-effect free, so inspection never creates anything, and a
    #: profile with one of these renders its real argv only at `start()`.
    prepare_spawn: Callable[[TurnContext], None] | None = None
    #: The one-shot counterpart of `prepare_spawn`.
    prepare_oneshot: Callable[[OneShotContext], None] | None = None
    #: Delete whatever this harness keeps on disk for one conversation, given
    #: the owning agent id and the session's resume id. A harness whose engine
    #: stores sessions itself needs this on Octopus's hard session delete —
    #: DSH has no deletion API of its own (dsh-harness.md §3.5). Best-effort by
    #: contract: a failure here must never fail the delete.
    cleanup_session: Callable[[str | None, str | None], None] | None = None
    # Lowercased substrings that identify an auth-credential rejection in
    # THIS backend's CLI error output (harness-credential-reauth.md §3). A
    # failed turn whose combined error text contains any of them is treated
    # as an expired/invalid credential; `Harness.is_auth_error` matches them.
    # Empty tuple = no reactive auth detection for this backend.
    auth_error_patterns: tuple[str, ...] = ()
    # Web search/fetch capability for native deep research (§4); None = the
    # backend has no web tools, so research is gated off on it.
    web: "WebCapability | None" = None
    # Lowercased substrings that identify a TRANSIENT provider-reliability
    # failure (5xx / overloaded / dropped connection / timeout) in this
    # backend's CLI error output (harness-transient-retry.md §3). A failed
    # turn matching these is retried with backoff. Must stay free of auth
    # phrases (handled separately) and quota/credit phrases (never retried).
    transient_error_patterns: tuple[str, ...] = ()

    # Phrases meaning "the resume id this session is pinned to no longer
    # exists on this engine" — its local transcript was rotated, cleaned or
    # written by another machine. Distinct from auth (the credential is fine)
    # and from transient (retrying the same id fails forever): the only way
    # out is to drop the id and start a fresh engine-side conversation, which
    # `SessionManager._run_backend` does exactly once per turn.
    stale_session_patterns: tuple[str, ...] = ()
    # Whether the composed system prompt should carry the agent-memory blurb
    # (docs/plans/memory.md §3). Codex: True (no native memory — it reads/
    # writes the canonical dir with file tools by instruction). Claude: False
    # (native memory, pointed at the canonical dir via an env override).
    injects_memory_prompt: bool = False
    # Whether this backend can be forked (session-rewind.md §3). A
    # backend supplies `prepare_fork` + a working resume strategy
    # (NATIVE_TRANSCRIPT or HISTORY_REPLAY). Both v1 backends set True; a
    # future backend with no strategy leaves it False and the "Fork from
    # here" affordance renders disabled. Surfaced via `SessionInfo.can_fork`.
    can_fork: bool = False
    # Collaborators (optional features):
    login: LoginDriver | None = None
    transcript_codec: TranscriptCodec | None = None
    # Fork strategy collaborators (session-rewind.md §3). `fork_prepare`
    # synthesizes backend-specific resume state and returns a `ForkArtifact`;
    # `fork_cleanup` sweeps any partial artifacts left by an incomplete saga.
    # Both async. None on a backend with no fork strategy (can_fork stays
    # False). Typed as Any to avoid importing the fork DTOs into this module.
    fork_prepare: Callable[..., Any] | None = None
    fork_cleanup: Callable[..., Any] | None = None
    # Full-copy fork (session-fork.md): copy the backend's NATIVE
    # transcript (the parent's real conversation file) into a fresh resumable id
    # at the fork's location, so a `/fork` duplicate continues with real context
    # instead of replaying the whole history into the first prompt. Returns a
    # `ForkArtifact`; falls back to `needs_replay=True` when the parent has no
    # native transcript yet. None on a backend with no native-copy strategy
    # (callers then fall back to replay). Async; Any to avoid importing DTOs.
    fork_copy: Callable[..., Any] | None = None
