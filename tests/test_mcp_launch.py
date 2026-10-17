"""Our own MCP servers must launch from *any* working directory.

They are spawned as `python -P -m server.mcp_servers.<name>` in the session's
working directory, and for an Application that directory is the app's own. An
ordinary Python app ships a `server.py` — so without `-P` Python prepends the
child's cwd to `sys.path`, the app's file shadows our `server` package, and
every built-in MCP server dies on start. The engine reports a dead MCP server
as a bare "Internal error", so the symptom is an agent that cannot answer at
all (found for real: `~/.octopus/applications/sapphire/server.py`).

The first test proves the shadow is real; the second proves our argv survives
it by completing an MCP handshake there.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

from server.harness import assembly

_DECOY = 'raise SystemExit("the application\'s own server.py was imported")\n'

_INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "octopus-test", "version": "1"},
    },
}
_INITIALIZED = {"jsonrpc": "2.0", "method": "notifications/initialized"}
_TOOLS_LIST = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}

_TIMEOUT = 60


async def _run(argv: list[str], cwd: Path, env: dict[str, str] | None = None):
    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=str(cwd),
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await asyncio.wait_for(proc.communicate(), timeout=_TIMEOUT)
    return proc.returncode, out.decode(errors="replace"), err.decode(errors="replace")


async def _handshake(argv: list[str], cwd: Path, env: dict[str, str]) -> dict:
    """Speak just enough MCP over stdio to prove the server is really up."""
    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=str(cwd),
        env=env,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    assert proc.stdin is not None and proc.stdout is not None

    async def _ask(frame: dict) -> None:
        proc.stdin.write((json.dumps(frame) + "\n").encode())
        await proc.stdin.drain()

    async def _read_id(want: int) -> dict:
        while True:
            line = await asyncio.wait_for(proc.stdout.readline(), timeout=_TIMEOUT)
            assert line, "the MCP server closed stdout before answering"
            try:
                frame = json.loads(line)
            except json.JSONDecodeError:
                continue  # a log line that leaked onto stdout
            if frame.get("id") == want:
                return frame

    try:
        await _ask(_INITIALIZE)
        answer = await _read_id(1)
        assert "result" in answer, answer
        await _ask(_INITIALIZED)
        await _ask(_TOOLS_LIST)
        listing = await _read_id(2)
        assert "result" in listing, listing
        return listing["result"]
    finally:
        proc.kill()
        await proc.wait()


@pytest.fixture
def app_dir(tmp_path: Path) -> Path:
    """A session working directory that looks like an app of ours."""
    (tmp_path / "server.py").write_text(_DECOY, encoding="utf-8")
    (tmp_path / "start.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    return tmp_path


async def test_the_decoy_really_shadows_our_package(app_dir: Path):
    """The premise, asserted rather than assumed: from that directory, a plain
    `import server` *runs the app's file*. Without this the test below could
    pass for the wrong reason on a machine where the package is installed.

    The decoy's own `SystemExit` is the proof it was executed — a resolved
    `server/__init__.py` would have imported cleanly.
    """
    rc, _, err = await _run(
        [sys.executable, "-c", "import server"],
        cwd=app_dir,
        env=dict(os.environ),
    )
    assert rc != 0
    assert "the application's own server.py was imported" in err


async def test_a_builtin_mcp_server_starts_in_an_apps_directory(app_dir: Path):
    """The built-in `bg` server answers `tools/list` with its cwd set to an app
    directory that ships `server.py`.

    Drop the `-P` from `assembly.mcp_module_argv` and this fails with
    `No module named 'server.mcp_servers'; 'server' is not a package` — which is
    exactly what the user saw as "Internal error".
    """
    entry = next(
        e
        for e in assembly.select_mcp_servers(
            ["bg"], [], assembly.build_callback_env("test-session")
        )
        if e.key == "bg"
    )
    env = {**os.environ, **entry.env}
    result = await _handshake([entry.command, *entry.args], app_dir, env)
    names = {tool["name"] for tool in result["tools"]}
    assert "run" in names, names
