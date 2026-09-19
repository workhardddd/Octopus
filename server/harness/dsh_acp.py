"""The ACP client the DSH harness is driven through (docs/plans/dsh-harness.md §3.3).

DSH's `acp` profile is an Agent Client Protocol v1 server over
newline-delimited JSON-RPC 2.0 on stdio. Octopus implements the client side
itself rather than taking an ACP SDK dependency — the same standing decision
that keeps the `claude`/`codex` streams hand-parsed ("CLIs, not an SDK",
docs/architecture.md) — and implements only the calls this integration needs:

    initialize                                     → server capabilities
    session/new     {cwd, mcpServers}              → {sessionId}
    session/resume  {sessionId, cwd, mcpServers}   → {sessionId}
    session/set_config_option {sessionId, configId, value}
    session/prompt  {sessionId, prompt}            → settles when the turn ends
    session/cancel  {sessionId}                    → (notification)

    session/update              (notification) → committed content + tool lifecycle
    session/request_permission  (server request) → we must answer it

Frame shapes are taken from the ACP v1 schema (`@agentclientprotocol/sdk`),
not from prose. Three wire facts are load-bearing and easy to get wrong:

* `mcpServers` is REQUIRED on `session/new`, and must be re-sent on
  `session/resume` — omitting it there leaves the resumed session with no MCP
  at all.
* `session/set_config_option` carries a *string* value, and the `model`
  option's value is an opaque `JSON.stringify([provider, model])` pair, so the
  driver composes exactly that.
* `session/request_permission` has no timeout anywhere on the path, so a
  request that arrives must always be answered (see `on_request`).
"""

from __future__ import annotations

import json
import logging
from typing import Any

from .. import dsh_home
from .events import HarnessEvent
from .profile import (
    EventParser,
    FrameKind,
    McpServerEntry,
    ParseOutput,
    TerminalProtocol,
    TurnContext,
)
from .run import ProtocolRequestError

logger = logging.getLogger(__name__)

#: The ACP version this client speaks (v1).
PROTOCOL_VERSION = 1

#: JSON-RPC's "method not found".
_METHOD_NOT_FOUND = -32601

#: The provider route a bare model id belongs to, and the model Octopus selects
#: when the agent names none. Both live with the settings that declare them —
#: see `server/dsh_home.py` for why the model has to be selected over ACP at all
#: (DSH's shipped ACP patch pins a different, text-only one).
DEFAULT_PROVIDER = dsh_home.DEFAULT_PROVIDER
DEFAULT_MODEL = dsh_home.DEFAULT_MODEL

#: Tool statuses that end a call (ACP `ToolCallStatus`).
_TERMINAL_TOOL_STATUS = {"completed", "failed"}

#: Stop reasons that mean the turn ended for a reason the user should see.
_NOTABLE_STOP_REASONS = {"max_tokens", "max_turn_requests", "refusal"}


def acp_mcp_servers(entries: list[McpServerEntry]) -> list[dict[str, Any]]:
    """Octopus's neutral MCP entries → ACP's `mcpServers` array.

    stdio is the only transport we speak, and ACP's stdio shape carries the
    environment as name/value pairs rather than a mapping.
    """
    return [
        {
            "name": entry.key,
            "command": entry.command,
            "args": list(entry.args),
            "env": [{"name": k, "value": v} for k, v in entry.env.items()],
        }
        for entry in entries
    ]


def model_option_value(model: str) -> str:
    """`ctx.model` → ACP's opaque model config value.

    A bare id belongs to the default route; `provider/model` names the route
    explicitly. Serialized with JS-compatible separators because the value IS
    `JSON.stringify([provider, model])` on the wire.
    """
    provider, slash, name = model.partition("/")
    pair = [provider, name] if slash else [DEFAULT_PROVIDER, provider]
    return json.dumps(pair, separators=(",", ":"))


def _text_of(content: Any) -> str:
    """The text of one ACP content block, or '' for anything else.

    Images, audio and resource links are not part of Octopus's transcript
    vocabulary, so they contribute nothing rather than a placeholder the UI
    would have to learn.
    """
    if isinstance(content, dict) and content.get("type") == "text":
        return str(content.get("text") or "")
    return ""


def _render_tool_content(update: dict[str, Any]) -> str:
    """A tool call's output as transcript text.

    Prefers the raw output the runtime reported; falls back to the text blocks
    inside its content list. A diff or terminal handle renders as nothing —
    inventing a rendering for them would put text in the transcript that the
    runtime never produced.
    """
    raw = update.get("rawOutput")
    if isinstance(raw, str):
        return raw
    if raw is not None:
        try:
            return json.dumps(raw)
        except TypeError:
            logger.debug("unserializable rawOutput on tool call", exc_info=True)
    parts = [
        _text_of(item.get("content"))
        for item in update.get("content") or []
        if isinstance(item, dict) and item.get("type") == "content"
    ]
    return "\n".join(p for p in parts if p)


def _tool_event(update: dict[str, Any]) -> HarnessEvent:
    return HarnessEvent(
        type="tool_use",
        tool_name=update.get("name") or update.get("title") or "",
        tool_input=update.get("rawInput") if isinstance(update.get("rawInput"), dict) else None,
        tool_use_id=update.get("toolCallId"),
    )


def _tool_result_event(update: dict[str, Any]) -> HarnessEvent:
    return HarnessEvent(
        type="tool_result",
        tool_use_id=update.get("toolCallId"),
        content=_render_tool_content(update),
        is_error=update.get("status") == "failed",
    )


class DshEventParser(EventParser):
    """ACP `session/update` notifications → the neutral event vocabulary.

    ACP carries *committed* content only — DSH's own README says raw provider
    deltas stay off the wire — so this parser never emits `text_delta`: text
    appears once it is a message (dsh-harness.md §10.1). Sub-agent runs have no
    ACP representation either and arrive as the ordinary tool call that spawned
    them (§10.3), so no `subagent` event is emitted.

    One assistant message arrives as one or more `agent_message_chunk`s that
    share a `messageId` ("a change in messageId indicates a new message has
    started"), so chunks are buffered and emitted as a single `text` event per
    message — flushed when the id changes, before any other kind of event, and
    by the engine just before the turn's terminal event. A non-terminal
    `tool_call_update` (progress) emits nothing: the transcript's tool card is
    written once by the `tool_call` and closed by a terminal update.
    """

    def __init__(self) -> None:
        self._message_id: str | None = None
        self._text: list[str] = []

    def _flush_text(self) -> list[HarnessEvent]:
        if not self._text:
            return []
        events = [HarnessEvent(type="text", content="".join(self._text))]
        self._text = []
        return events

    def flush(self) -> ParseOutput:
        return ParseOutput(events=self._flush_text())

    def parse(self, obj: dict[str, Any]) -> ParseOutput:
        if obj.get("method") != "session/update":
            return ParseOutput()
        update = (obj.get("params") or {}).get("update") or {}
        if not isinstance(update, dict):
            return ParseOutput()
        kind = update.get("sessionUpdate")

        if kind == "agent_message_chunk":
            message_id = update.get("messageId")
            events: list[HarnessEvent] = []
            if message_id != self._message_id:
                events += self._flush_text()
                self._message_id = message_id
            text = _text_of(update.get("content"))
            if text:
                self._text.append(text)
            return ParseOutput(events=events)

        # Anything that is not a message chunk ends the message being buffered.
        events = self._flush_text()
        self._message_id = None

        if kind == "agent_thought_chunk":
            text = _text_of(update.get("content"))
            if text:
                events.append(HarnessEvent(type="thinking", content=text))
        elif kind == "tool_call":
            events.append(_tool_event(update))
            if update.get("status") in _TERMINAL_TOOL_STATUS:
                events.append(_tool_result_event(update))
        elif kind == "tool_call_update":
            if update.get("status") in _TERMINAL_TOOL_STATUS:
                events.append(_tool_result_event(update))
        return ParseOutput(events=events)


def _error_text(error: Any) -> str:
    if isinstance(error, dict):
        message = error.get("message") or ""
        code = error.get("code")
        return f"{message} (code {code})" if code is not None else str(message)
    return str(error)


def _reject_option(params: dict[str, Any]) -> str | None:
    """The optionId to answer a permission request with.

    We always decline: the turns Octopus runs are unattended, and the DSH
    profile it renders sets `approval: never` so this path should not be
    reached at all. Picking an option *the request actually offered* is what
    keeps the answer valid if one ever arrives.
    """
    options = params.get("options") or []
    for preferred in ("reject_once", "reject_always"):
        for option in options:
            if isinstance(option, dict) and option.get("kind") == preferred:
                return option.get("optionId")
    return None


class DshAcpProtocol(TerminalProtocol):
    """One DSH conversation over ACP.

    A fresh instance per run (the profile's `new_protocol` factory), because it
    holds the turn in flight and the session id it was handed.
    """

    def __init__(self) -> None:
        self._next_id = 0
        self._session_id: str | None = None
        self._turn_id: Any = None

    def _id(self) -> int:
        self._next_id += 1
        return self._next_id

    def _request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": self._id(), "method": method, "params": params}

    async def handshake(self, run, ctx: TurnContext) -> str | None:
        await run.request(
            self._request(
                "initialize",
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    "clientCapabilities": {},
                    "clientInfo": {"name": "octopus", "version": "0.1"},
                },
            )
        )
        mcp_servers = acp_mcp_servers(ctx.mcp_servers)
        if ctx.resume_id:
            # The MCP declarations must be re-sent here: a resume without them
            # composes a session with no MCP servers at all.
            try:
                result = await run.request(
                    self._request(
                        "session/resume",
                        {
                            "sessionId": ctx.resume_id,
                            "cwd": ctx.working_dir,
                            "mcpServers": mcp_servers,
                        },
                    )
                )
            except ProtocolRequestError as exc:
                # A resume this engine cannot serve is a *stale session*, and it
                # has to be recognised as one whatever DSH calls it. Its answer
                # is often a catch-all — a store its own abrupt kill left
                # unreadable comes back as a bare "Internal error" — and the
                # turn-level recovery only fires on a message it can classify,
                # so an unclassified failure here bricked the session: every
                # later turn re-tried the same dead id and failed identically
                # (found in a trial, twice in a row). Say what it is, in the
                # wording the classifier already matches (verified against the
                # real CLI's own dangling-id error), and let Octopus drop the id
                # and start a fresh engine-side conversation.
                raise ProtocolRequestError(
                    f"session is not resumable: {ctx.resume_id} — {exc}"
                ) from exc
        else:
            result = await run.request(
                self._request(
                    "session/new",
                    {"cwd": ctx.working_dir, "mcpServers": mcp_servers},
                )
            )
        self._session_id = (result or {}).get("sessionId") or ctx.resume_id
        # Always select the route, even when the agent names no model: DSH's
        # shipped ACP config pins one no one chose (see DEFAULT_MODEL), and the
        # selection is what decides whether the bridge accepts image content.
        await run.request(
            self._request(
                "session/set_config_option",
                {
                    "sessionId": self._session_id,
                    "configId": "model",
                    "value": model_option_value(ctx.model or DEFAULT_MODEL),
                },
            )
        )
        return self._session_id

    async def send_turn(self, run, text: str) -> str | None:
        self._turn_id = self._id()
        await run.write_frame(
            {
                "jsonrpc": "2.0",
                "id": self._turn_id,
                "method": "session/prompt",
                "params": {
                    "sessionId": self._session_id,
                    "prompt": [{"type": "text", "text": text}],
                },
            }
        )
        return str(self._turn_id)

    def classify(self, obj: dict[str, Any]) -> FrameKind:
        if "method" in obj:
            return FrameKind.REQUEST if "id" in obj else FrameKind.EVENT
        return FrameKind.RESPONSE

    async def on_response(
        self, run, request_id: Any, result: Any, error: Any
    ) -> ParseOutput:
        """The prompt's response is the turn's settlement — ACP settles it
        after the agent goes idle and its updates are delivered, so it is the
        one frame that ends the stream."""
        if request_id != self._turn_id:
            return ParseOutput()
        self._turn_id = None
        if error is not None:
            return ParseOutput(
                events=[
                    HarnessEvent(
                        type="error",
                        content=_error_text(error),
                        is_error=True,
                        session_id=self._session_id,
                        raw=error if isinstance(error, dict) else None,
                    )
                ],
                end_of_stream=True,
            )
        stop = (result or {}).get("stopReason")
        if stop in _NOTABLE_STOP_REASONS:
            # A settlement the user should be able to explain: the turn did end
            # (so it is a `result`, not a failure), but not because the model
            # was finished.
            logger.warning(
                "DSH turn on session %s settled with stopReason=%s",
                self._session_id,
                stop,
            )
        return ParseOutput(
            events=[
                HarnessEvent(
                    type="result",
                    session_id=self._session_id,
                    raw=result if isinstance(result, dict) else None,
                )
            ],
            end_of_stream=True,
        )

    async def on_request(
        self, run, request_id: Any, method: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        if method == "session/request_permission":
            option_id = _reject_option(params)
            outcome: dict[str, Any] = (
                {"outcome": "selected", "optionId": option_id}
                if option_id
                else {"outcome": "cancelled"}
            )
            logger.info(
                "DSH asked for permission on session %s; declining (%s)",
                self._session_id,
                option_id or "cancelled",
            )
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {"outcome": outcome},
            }
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {
                "code": _METHOD_NOT_FOUND,
                "message": f"unsupported method: {method}",
            },
        }

    async def cancel(self, run) -> None:
        """`session/cancel` first (ACP's own cancellation path, a
        notification), then stop the process — killing it without cancelling
        would abandon a turn the runtime still holds."""
        if self._session_id:
            try:
                await run.write_frame(
                    {
                        "jsonrpc": "2.0",
                        "method": "session/cancel",
                        "params": {"sessionId": self._session_id},
                    }
                )
            except Exception:
                logger.debug("session/cancel could not be sent", exc_info=True)
        await run.stop()
