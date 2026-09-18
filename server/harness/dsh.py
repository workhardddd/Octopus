"""The DSH runtime profile (docs/plans/dsh-harness.md).

Everything DSH-specific as data + collaborators behind the `DSH`
`RuntimeProfile`: rendering a turn into a `dsh --profile acp` process driven
over ACP (`dsh_acp.py`), normalizing its committed updates, the one-shot call
(`dsh --profile headless "<task>"`, whose stdout IS the answer), and the
per-agent environment (a `DSH_HOME` per agent, the credential injected as
`DEEPSEEK_API_KEY`).

Two declared gaps, both consequences of the ACP surface rather than of this
profile: there is no token-level streaming (ACP carries committed content
only) and no per-turn tool policy (DSH's tools are composed per process). The
plan's §10 is the authoritative list; `RuntimeProfile`'s derived predicates
(`can_steer`, `can_export`, `can_import`) express the rest.
"""

from __future__ import annotations

import hashlib
import logging
import os
from typing import Any

from .dsh_acp import DshAcpProtocol, DshEventParser
from .events import HarnessCredential, HarnessEvent, SubagentUpdate  # noqa: F401
from .harness import Harness
from .profile import (
    OneShotContext,
    RuntimeProfile,
    StdinMode,
    TurnContext,
    WebCapability,
)
from .registry import register

logger = logging.getLogger(__name__)

#: The CLI, and the two of its own profiles Octopus uses: `acp` for turns (the
#: only surface with per-session cwd + MCP + cancellation) and `headless` for
#: one-shots (one task in, its answer on stdout, process exits).
BINARY = "dsh"
ACP_PROFILE = "acp"
HEADLESS_PROFILE = "headless"


# DSH variant of the in-app-tools system prompt (same builtins as Claude and
# Codex, phrased for DSH's execution model). Rendered into the generated
# patch's `personaPrefix` every spawn (dsh-harness.md §3.5) — DSH takes prompt
# text by composition, never as a runtime argument.
_OCTOPUS_SYSTEM_PROMPT_DSH = """\
== Octopus in-app tools ==

You have access to extra tools injected by the Octopus controller. They are \
first-class — call them whenever appropriate, not as a fallback.

[1] `mcp__bg__run(command, description?)` — fire-and-forget a shell command \
that runs in the BACKGROUND across turns. Returns a task_id immediately. When \
it finishes, Octopus injects a follow-up turn into this session with the \
captured output, and you respond then. Use it for anything long-running or \
unbounded — test suites, builds, package installs, sleeps, large fetches.

This is **not** your own background-job tool: a job you start with your own \
tools reports into this conversation only if you come back and read it, and \
your turn ends before that happens. Only `mcp__bg__run` delivers the result \
back as a new turn, so anything you want to hear about again goes through it. \
Related: `mcp__bg__cancel(task_id)`, `mcp__bg__list()`.

[2] `mcp__ask__user(questions)` — ask the user clarification questions and \
BLOCK until they answer. Use it when a real choice depends on the user and \
there isn't an obviously right answer; don't use it for things you can decide \
yourself or verify from the workspace.

[3] `mcp__ask_agent__ask(name, request, files?)` — delegate to another \
Octopus agent by display name. When the user says "ask <name> to …", \
"delegate this to <name>", or "have <name> review …", that is a direct \
call to invoke this tool — don't paraphrase, just call it with a \
self-contained `request` (the other agent never sees this transcript). \
Returns immediately; the other agent's reply arrives later as a follow-up \
turn prefixed `[agent-reply:<name> delegation=<id>]` (or `[agent-question:…]` \
/ `[agent-error:…]`). If a question arrives, answer it via \
`mcp__ask_agent__answer(delegation_id, choice)` when you can, or ask \
the user via `mcp__ask__user` if you can't. For follow-up rounds \
with the same agent on the same line of work (review iterations, \
"apply that same review to file Y"), call `mcp__ask_agent__ask` \
again but pass the PRIOR `delegation_id` (and omit `name`) — the \
same child session is reused so the other agent keeps their \
transcript and doesn't re-read from scratch. Exactly one of (`name`, \
`delegation_id`) must be set. Related: `mcp__ask_agent__cancel`, \
`mcp__ask_agent__list`."""


# A DSH turn is unusable without the generated patch: the patch carries the
# agent's persona AND the permission posture. Without it a stock `acp` profile
# asks for approval, and that request has no timeout on the ACP path — a turn
# could sit on it forever (dsh-harness.md §7 risk 3). A one-shot is exempt:
# `headless` has no blocking approval channel (an unanswered request fails
# closed), and it needs no persona.
_MISSING_HOME = (
    "the dsh harness needs a DSH_HOME for every turn — without one the "
    "process writes Octopus's sessions into the user's own ~/.dsh "
    "(docs/plans/dsh-harness.md §3.5)"
)
_MISSING_PATCH = (
    "a dsh turn needs the patch generated for this spawn: it carries both the "
    "agent's persona and the permission posture "
    "(docs/plans/dsh-harness.md §3.5)"
)


def _dsh_env(
    dsh_home: str | None, credential: HarnessCredential | None
) -> dict[str, str]:
    """The child environment for a DSH process.

    `DSH_HOME` has to be *exported* — DSH refuses `DSH_*` variables from a
    `.env` file — and it is the single isolation knob: sessions, settings,
    credentials, profiles and the user-global instruction file all travel with
    it.
    """
    if not dsh_home:
        raise ValueError(_MISSING_HOME)
    env = os.environ.copy()
    env["DSH_HOME"] = dsh_home
    # The launch environment outranks every credentials file DSH reads, so an
    # injected key means the agent's home never holds a copy of it at rest.
    if credential is not None and credential.secret:
        env["DEEPSEEK_API_KEY"] = credential.secret
    return env


def build_turn_argv(ctx: TurnContext) -> tuple[list[str], dict[str, Any]]:
    """`dsh --profile acp [--patch <generated>]`, with the per-agent
    environment."""
    if not ctx.dsh_patch:
        raise ValueError(_MISSING_PATCH)
    argv = [BINARY, "--profile", ACP_PROFILE, "--patch", ctx.dsh_patch]
    env = _dsh_env(ctx.dsh_home, ctx.credential)
    # No prompt in argv and no prompt on a raw stdin frame: the turn is a
    # `session/prompt` request the protocol sends after the handshake.
    return argv, {"cwd": ctx.working_dir, "env": env}


def build_oneshot_argv(ctx: OneShotContext) -> tuple[list[str], dict[str, Any]]:
    """`dsh --profile headless "<task>"` — one task, its answer on stdout."""
    argv = [BINARY, "--profile", HEADLESS_PROFILE, ctx.prompt]
    kwargs: dict[str, Any] = {"env": _dsh_env(ctx.dsh_home, ctx.credential)}
    if ctx.working_dir:
        kwargs["cwd"] = ctx.working_dir
    return argv, kwargs


def parse_oneshot_stdout(stdout: str) -> str:
    """The final answer is stdout itself; DSH puts its reasoning on stderr."""
    return stdout.strip()


# Lowercased substrings that identify an auth-credential rejection
# (harness-credential-reauth.md §3). Verified against a real rejected request
# by tests/test_backend_dsh_real.py, which runs one turn with a deliberately
# invalid key — a guessed pattern list would silently turn the re-auth feature
# into a no-op, so it is exercised rather than assumed.
_DSH_AUTH_ERROR_PATTERNS = (
    "401",
    "unauthorized",
    "authentication fails",
    "invalid api key",
    "invalid_api_key",
    "incorrect api key",
)

# Transient provider/transport failures (harness-transient-retry.md §3). Kept
# free of auth phrases and of quota/credit phrases, which are never retried.
_DSH_TRANSIENT_ERROR_PATTERNS = (
    "overloaded",
    "500",
    "502",
    "503",
    "504",
    "529",
    "internal server error",
    "service unavailable",
    "bad gateway",
    "gateway timeout",
    "econnreset",
    "connection reset",
    "socket hang up",
    "fetch failed",
    "request timed out",
    "timed out",
)

# The resume id this session is pinned to no longer exists on this host:
# DSH's `session/resume` verifies the stored working directory against the
# absolute cwd it is given, and its store is machine- and cwd-bound
# (dsh-harness.md §0.1.11). The recovery is to drop the id once and start a
# fresh conversation.
#
# "session is not resumable" is the wording a real dangling id produced
# (tests/test_backend_dsh_real.py exercises it); the rest are the shapes DSH's
# session machinery is documented to use, kept because a pattern that never
# matches is harmless while a missing one makes the recovery a no-op.
_DSH_STALE_SESSION_PATTERNS = (
    "session is not resumable",
    "session cwd does not match",
    "unknown session",
    "session not found",
    "no such session",
)


def _cleanup_session(agent_id: str | None, resume_id: str | None) -> None:
    """Drop one conversation's DSH store (plan §3.5). Best-effort by contract:
    Octopus's session delete must not fail because DSH's files are gone, moved
    or locked."""
    if not agent_id:
        return
    from .. import dsh_home

    dsh_home.purge_session_store(agent_id, resume_id)


def _patch_signature(ctx: TurnContext) -> str:
    """A stable name for this spawn's patch.

    Everything the file's *content* depends on, hashed: two turns with the same
    inputs share one file (and it is rewritten in place), and anything that
    changes the persona, the memory the agent reads or the leaf scoping gets a
    name of its own.
    """
    parts = [ctx.system_prompt or "", ctx.memory_dir or "", str(ctx.web_research)]
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()[:16]


def _prepare_spawn(ctx: TurnContext) -> None:
    """Write this spawn's DSH home, memory view and patch (plan §3.5).

    Runs once per spawn, before the argv is rendered: the patch path set here
    is what `build_turn_argv` passes with `--patch`. A turn with no owning agent
    has no home to isolate and no memory to point at, so it is refused here
    rather than silently running against the user's own `~/.dsh`.
    """
    if not ctx.agent_id:
        raise ValueError(
            "a dsh turn needs an owning agent: its DSH home, memory view and "
            "generated patch are all derived from the agent "
            "(docs/plans/dsh-harness.md §3.5)"
        )
    from .. import dsh_home

    home = dsh_home.ensure_agent_home(ctx.agent_id)
    ctx.dsh_home = str(home)
    ctx.dsh_patch = str(
        dsh_home.write_patch(
            ctx.agent_id,
            _patch_signature(ctx),
            persona=ctx.system_prompt,
            memory_dir=ctx.memory_dir,
            web_research=ctx.web_research,
        )
    )


def _prepare_oneshot(ctx: OneShotContext) -> None:
    """A one-shot needs a home — so it never touches the user's own `~/.dsh` —
    but no patch: `headless` has no blocking approval channel to be trapped by,
    and a parse or synthesis call needs no persona."""
    if ctx.dsh_home:
        return
    from .. import dsh_home

    ctx.dsh_home = str(
        dsh_home.ensure_agent_home(ctx.agent_id)
        if ctx.agent_id
        else dsh_home.ensure_oneshot_home()
    )


async def _fork_prepare_replay(
    messages: list[Any],
    working_dir: str,
    resume_id_hint: str | None,
    fork_id: str,
) -> Any:
    """No on-disk work: DSH forks replay history into the first turn.

    `/rewind` needs no native transcript for any kind (dsh-harness.md §10.4),
    and `/fork` cannot use one either: it duplicates a session onto a *new*
    working directory, while DSH's `session/resume` verifies the stored cwd —
    so a copied session could never be resumed there (§3.6). Returning
    `needs_replay=True` is what makes the harness layer wrap the truncated
    history into the first turn's user message.
    """
    from .fork import ForkArtifact

    return ForkArtifact(resume_id=None, needs_replay=True)


DSH = RuntimeProfile(
    backend="dsh",
    binary=BINARY,
    tools_prompt=_OCTOPUS_SYSTEM_PROMPT_DSH,
    credential_style="env_secret",
    # The premature-exit-after-tool-roundtrip respawn is a Claude-CLI bug
    # workaround; DSH has no such failure mode to recover from.
    premature_exit_recovery=False,
    auth_error_patterns=_DSH_AUTH_ERROR_PATTERNS,
    transient_error_patterns=_DSH_TRANSIENT_ERROR_PATTERNS,
    stale_session_patterns=_DSH_STALE_SESSION_PATTERNS,
    # Separate search and fetch tools, so a research leaf can be allowed both
    # (native-deep-research.md §4). The leaf's *scoping* is a second generated
    # patch, because ACP cannot set a per-turn tool policy (§3.7).
    web=WebCapability(tool_names=("web_search", "web_fetch"), combined=False),
    # The turn is a request/response conversation: the protocol sets the
    # session up, delivers the prompt, and answers what DSH asks back.
    stdin_mode=StdinMode.PROTOCOL,
    new_protocol=DshAcpProtocol,
    prepare_spawn=_prepare_spawn,
    prepare_oneshot=_prepare_oneshot,
    cleanup_session=_cleanup_session,
    build_turn_argv=build_turn_argv,
    new_event_parser=DshEventParser,
    build_oneshot_argv=build_oneshot_argv,
    parse_oneshot_stdout=parse_oneshot_stdout,
    # DSH reads the per-agent memory directory itself: the generated patch
    # points its `agent-instructions` row at it, so the prompt needs no blurb
    # (unlike Codex, which has no such surface).
    injects_memory_prompt=False,
    can_fork=True,  # /rewind and /fork both replay history (fork_copy is None)
    fork_prepare=_fork_prepare_replay,
    fork_copy=None,
    login=None,  # an API key, pasted — no login flow to drive
    transcript_codec=None,  # no handoff/pull: DSH's store is its own format
)

register(Harness(DSH))
