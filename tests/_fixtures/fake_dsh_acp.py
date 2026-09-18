#!/usr/bin/env python3
"""A fake `dsh --profile acp` for the DSH harness tests.

Speaks the same newline-delimited JSON-RPC 2.0 as the real ACP server, so the
profile's handshake, its event mapping and its answer path can be exercised
without the real CLI or a DeepSeek key.

Every frame the client sends is appended to the record file named by argv[1]
(JSON Lines), which is how a test asserts what actually went on the wire — the
`mcpServers` array on `session/new`, the opaque model value, the answer to a
permission request.

Modes (argv[2]):
  turn  (default) : one full turn — a thought, two text chunks, a tool call
                    that completes, a permission request the client must
                    answer, then the prompt's settlement.
  slow            : emit one update and wait; the turn settles only when the
                    client sends `session/cancel`.
  error           : settle the prompt with a JSON-RPC error.
"""

import json
import sys
from typing import Any


def main() -> int:
    record_path = sys.argv[1] if len(sys.argv) > 1 else ""
    mode = sys.argv[2] if len(sys.argv) > 2 else "turn"

    def record(obj: dict[str, Any]) -> None:
        if not record_path:
            return
        with open(record_path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(obj) + "\n")

    def write(obj: dict[str, Any]) -> None:
        sys.stdout.write(json.dumps(obj) + "\n")
        sys.stdout.flush()

    def result(request_id: Any, payload: Any) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id, "result": payload}

    def update(session_id: str | None, payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {"sessionId": session_id, "update": payload},
        }

    session_id: str | None = None
    pending_prompt: Any = None

    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except ValueError:
            continue
        record(obj)

        method = obj.get("method")
        params = obj.get("params") or {}

        if method is None:
            # A response from the client. The only one that matters is the
            # answer to our permission request: the turn settles after it, so a
            # test can prove the answer arrived before settlement.
            if pending_prompt is not None and str(obj.get("id")).startswith("perm-"):
                write(result(pending_prompt, {"stopReason": "end_turn"}))
                pending_prompt = None
            continue

        request_id = obj.get("id")

        if method == "initialize":
            write(result(request_id, {"protocolVersion": 1, "agentCapabilities": {}}))
        elif method == "session/new":
            session_id = "dsh-sess-1"
            write(result(request_id, {"sessionId": session_id}))
        elif method == "session/resume":
            session_id = params.get("sessionId") or "dsh-sess-1"
            write(result(request_id, {"sessionId": session_id}))
        elif method == "session/set_config_option":
            write(
                result(
                    request_id,
                    {
                        "configOptions": [
                            {"id": params.get("configId"), "currentValue": params.get("value")}
                        ]
                    },
                )
            )
        elif method == "session/prompt":
            pending_prompt = request_id
            if mode == "error":
                write(
                    {
                        "jsonrpc": "2.0",
                        "id": pending_prompt,
                        "error": {"code": -32001, "message": "model exploded"},
                    }
                )
                pending_prompt = None
            elif mode == "slow":
                write(
                    update(
                        session_id,
                        {
                            "sessionUpdate": "agent_message_chunk",
                            "content": {"type": "text", "text": "working"},
                        },
                    )
                )
            else:
                write(
                    update(
                        session_id,
                        {
                            "sessionUpdate": "agent_thought_chunk",
                            "content": {"type": "text", "text": "thinking"},
                        },
                    )
                )
                write(
                    update(
                        session_id,
                        {
                            "sessionUpdate": "agent_message_chunk",
                            "content": {"type": "text", "text": "Hello "},
                        },
                    )
                )
                write(
                    update(
                        session_id,
                        {
                            "sessionUpdate": "agent_message_chunk",
                            "content": {"type": "text", "text": "world"},
                        },
                    )
                )
                write(
                    update(
                        session_id,
                        {
                            "sessionUpdate": "tool_call",
                            "toolCallId": "tc-1",
                            "title": "Read a.txt",
                            "name": "read",
                            "status": "pending",
                            "rawInput": {"path": "a.txt"},
                        },
                    )
                )
                write(
                    update(
                        session_id,
                        {
                            "sessionUpdate": "tool_call_update",
                            "toolCallId": "tc-1",
                            "status": "completed",
                            "rawOutput": "file body",
                        },
                    )
                )
                write(
                    {
                        "jsonrpc": "2.0",
                        "id": "perm-1",
                        "method": "session/request_permission",
                        "params": {
                            "sessionId": session_id,
                            "toolCall": {"toolCallId": "tc-1"},
                            "options": [
                                {"optionId": "allow-once", "name": "Allow once", "kind": "allow_once"},
                                {"optionId": "reject-once", "name": "Reject", "kind": "reject_once"},
                            ],
                        },
                    }
                )
        elif method == "session/cancel":
            # A notification (no id): settle the turn it cancels.
            if pending_prompt is not None:
                write(result(pending_prompt, {"stopReason": "cancelled"}))
                pending_prompt = None

    return 0


if __name__ == "__main__":
    sys.exit(main())
