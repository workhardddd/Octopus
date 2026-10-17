"""The harness layer — the single boundary for all model/runtime interaction.

One `Harness` class configured by a `RuntimeProfile` value per backend
kind (no per-framework subclasses). See docs/plans/harness-layer.md, and
docs/plans/dsh-harness.md for the `PROTOCOL` stdin mode a request/response
CLI (DSH over ACP) drives the engine through.

Profiles self-register on import; importing this package wires them up
(the claude_code/codex imports below run their `register(...)` calls).
"""

from __future__ import annotations

from .events import (
    HarnessCredential,
    HarnessEvent,
    HarnessOneshotError,
    SubagentUpdate,
)
from .fork import (
    HISTORY_REPLAY,
    NATIVE_TRANSCRIPT,
    BackendForkNotSupported,
    ForkArtifact,
)
from .harness import Harness
from .login import LoginDriver, LoginMethod
from .profile import (
    EventParser,
    FrameKind,
    McpServerEntry,
    OneShotContext,
    ParseOutput,
    RuntimeProfile,
    StdinMode,
    TerminalProtocol,
    TranscriptCodec,
    TurnContext,
    WebCapability,
)
from .registry import (
    DEFAULT_BACKEND,
    all_backends,
    available_backends,
    get_harness,
    has_backend,
    register,
)
from .run import HarnessRun, ProtocolRequestError, RunConfig

__all__ = [
    "HarnessCredential",
    "HarnessEvent",
    "SubagentUpdate",
    "HarnessOneshotError",
    "Harness",
    "HarnessRun",
    "ProtocolRequestError",
    "RunConfig",
    "RuntimeProfile",
    "StdinMode",
    "TerminalProtocol",
    "FrameKind",
    "TurnContext",
    "WebCapability",
    "OneShotContext",
    "McpServerEntry",
    "EventParser",
    "ParseOutput",
    "TranscriptCodec",
    "ForkArtifact",
    "BackendForkNotSupported",
    "NATIVE_TRANSCRIPT",
    "HISTORY_REPLAY",
    "LoginDriver",
    "LoginMethod",
    "register",
    "get_harness",
    "has_backend",
    "all_backends",
    "available_backends",
    "DEFAULT_BACKEND",
]

# Profile registration: importing the profile modules runs their
# `register(Harness(...))` side effects.
from . import claude_code  # noqa: E402,F401  (registers the claude-code harness)
from . import codex  # noqa: E402,F401  (registers the codex harness)
from . import dsh  # noqa: E402,F401  (registers the dsh harness)
