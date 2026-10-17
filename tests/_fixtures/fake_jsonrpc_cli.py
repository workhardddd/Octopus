#!/usr/bin/env python3
"""A fake JSON-RPC-over-stdio CLI for the `PROTOCOL` stdin mode.

A protocol harness (DSH over ACP — docs/plans/dsh-harness.md §3.1) speaks
newline-delimited JSON-RPC 2.0 on stdin/stdout rather than emitting one event
per line, so the engine's frame routing, request correlation and
answer-write path need a stand-in that behaves that way. This is it.

Modes (argv[1]):
  session : answer `initialize`; answer `session/new` with `{"sessionId": …}`;
            then for each `session/prompt` emit a `session/update`
            notification, ask the *client* one `ask` request, and — only once
            the client answers it — emit a second update and settle the
            prompt with a result. Proving the prompt response is what ends a
            turn, and that a server→client request is answered rather than
            ignored.
  error   : answer the first request with a JSON-RPC error.
  die     : read one request, then exit without answering it, so a request in
            flight is never answered and the pipe closes under it.
"""

import json
import sys
from typing import Any


def _write(obj: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def _result(request_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def _notify(method: str, params: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "method": method, "params": params}


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "session"

    if mode == "die":
        # Read one request first so the client's write succeeds, then exit
        # without answering: the client must notice the closed pipe rather
        # than wait on a response that can no longer come.
        sys.stdin.readline()
        return 0

    #: The prompt this client still owes an answer to, held until the server
    #: request below is answered — the ordering a real engine settles on.
    waiting_turn: Any = None
    first_request_seen = False

    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except ValueError:
            continue

        if "method" not in obj:
            # A response from the client. Only one is interesting: the answer
            # to our `ask`, which releases the turn it belongs to.
            if waiting_turn is not None and str(obj.get("id")) == "srv-ask":
                _write(_notify("session/update", {"seq": 2, "answer": obj.get("result")}))
                _write(_result(waiting_turn, {"stopReason": "end_turn"}))
                waiting_turn = None
            continue

        method = obj.get("method")
        request_id = obj.get("id")

        if mode == "error" and not first_request_seen:
            first_request_seen = True
            _write(_error(request_id, -32000, "no thanks"))
            continue

        if method == "initialize":
            _write(_result(request_id, {"serverInfo": {"name": "fake"}}))
        elif method == "session/new":
            params = obj.get("params") or {}
            _write(_result(request_id, {"sessionId": "sess-1", "cwd": params.get("cwd")}))
        elif method == "session/prompt":
            _write(_notify("session/update", {"seq": 1, "text": (obj.get("params") or {}).get("text")}))
            waiting_turn = request_id
            _write({"jsonrpc": "2.0", "id": "srv-ask", "method": "ask", "params": {"q": "?"}})
        else:
            _write(_error(request_id, -32601, f"unknown method: {method}"))

    return 0


if __name__ == "__main__":
    sys.exit(main())
