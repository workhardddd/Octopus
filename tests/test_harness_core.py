"""Phase 1 harness-layer tests: the merged run engine, shared assembly, the
registry, and run_oneshot — all driven by a fake RuntimeProfile + the shared
fake CLI, so they don't need a real claude/codex binary.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from server.harness import (
    EventParser,
    FrameKind,
    Harness,
    HarnessEvent,
    HarnessOneshotError,
    OneShotContext,
    ParseOutput,
    ProtocolRequestError,
    RunConfig,
    RuntimeProfile,
    StdinMode,
    TerminalProtocol,
    TurnContext,
    available_backends,
    get_harness,
    register,
)
from server.harness import assembly
from server.harness.registry import _REGISTRY

FAKE_CLI = Path(__file__).parent / "_fixtures" / "fake_cli.py"


# --------------------------------------------------------------------------- #
# Fake profile
# --------------------------------------------------------------------------- #


class _RawParser(EventParser):
    """Emits one event per stdout object (type from `type`, raw=obj); ends the
    stream when a `result` object arrives — mirrors the real terminal-event
    contract."""

    def parse(self, obj: dict[str, Any]) -> ParseOutput:
        ev = HarnessEvent(type=obj.get("type", "?"), raw=obj)
        return ParseOutput(events=[ev], end_of_stream=obj.get("type") == "result")


def _stream_profile(
    *lines: str, stdin_mode: StdinMode = StdinMode.STREAM_JSON
) -> RuntimeProfile:
    def build_turn_argv(ctx: TurnContext) -> tuple[list[str], dict[str, Any]]:
        return ([sys.executable, str(FAKE_CLI), "emit-lines", *lines], {"cwd": ctx.working_dir})

    return RuntimeProfile(
        backend="fake",
        binary=sys.executable,
        tools_prompt="TOOLS",
        credential_style="env_secret",
        premature_exit_recovery=False,
        stdin_mode=stdin_mode,
        build_turn_argv=build_turn_argv,
        new_event_parser=_RawParser,
        build_oneshot_argv=lambda ctx: ([sys.executable], {}),
        parse_oneshot_stdout=lambda s: s,
    )


def _mode_profile(mode: str, *args: str) -> RuntimeProfile:
    """A streaming profile that runs the fake CLI in an arbitrary mode."""
    def build_turn_argv(ctx: TurnContext) -> tuple[list[str], dict[str, Any]]:
        return ([sys.executable, str(FAKE_CLI), mode, *args], {"cwd": ctx.working_dir})

    return RuntimeProfile(**{**_stream_profile().__dict__, "build_turn_argv": build_turn_argv})


async def _drain(run) -> list[HarnessEvent]:
    return [ev async for ev in run.stream()]


# --------------------------------------------------------------------------- #
# Engine: streaming + lifecycle
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_engine_streams_events_and_ends(tmp_path):
    profile = _stream_profile('{"type":"hello"}', '{"type":"result"}')
    run = Harness(profile).create_run(RunConfig())
    await run.start("p", str(tmp_path))
    events = await asyncio.wait_for(_drain(run), timeout=3.0)
    await run.stop()
    assert [e.type for e in events] == ["hello", "result"]


@pytest.mark.asyncio
async def test_engine_skips_malformed_lines(tmp_path):
    def build_turn_argv(ctx):
        return ([sys.executable, str(FAKE_CLI), "bad-json"], {"cwd": ctx.working_dir})

    profile = _stream_profile()
    profile = RuntimeProfile(**{**profile.__dict__, "build_turn_argv": build_turn_argv})
    run = Harness(profile).create_run()
    await run.start("p", str(tmp_path))
    events = await asyncio.wait_for(_drain(run), timeout=3.0)
    await run.stop()
    assert [e.type for e in events] == ["good"]


@pytest.mark.asyncio
async def test_engine_close_stdin_flag(tmp_path):
    # CLOSE_AFTER_SPAWN must not break a normal run (codex's behaviour).
    profile = _stream_profile(
        '{"type":"result"}', stdin_mode=StdinMode.CLOSE_AFTER_SPAWN
    )
    run = Harness(profile).create_run()
    await run.start("p", str(tmp_path))
    events = await asyncio.wait_for(_drain(run), timeout=3.0)
    await run.stop()
    assert [e.type for e in events] == ["result"]


@pytest.mark.asyncio
async def test_engine_starting_twice_raises(tmp_path):
    run = Harness(_stream_profile('{"type":"result"}')).create_run()
    await run.start("p", str(tmp_path))
    with pytest.raises(RuntimeError, match="already started"):
        await run.start("p", str(tmp_path))
    await run.stop()


@pytest.mark.asyncio
async def test_engine_missing_binary_raises(tmp_path):
    def build_turn_argv(ctx):
        return (["definitely-not-a-real-binary-12345"], {"cwd": ctx.working_dir})

    profile = RuntimeProfile(**{**_stream_profile().__dict__, "build_turn_argv": build_turn_argv})
    run = Harness(profile).create_run()
    with pytest.raises(FileNotFoundError, match="not found on PATH"):
        await run.start("p", str(tmp_path))


@pytest.mark.asyncio
async def test_engine_stop_idempotent(tmp_path):
    run = Harness(_stream_profile('{"type":"result"}')).create_run()
    await run.start("p", str(tmp_path))
    await asyncio.wait_for(_drain(run), timeout=3.0)
    await run.stop()
    await run.stop()  # second stop is a no-op, not an error


@pytest.mark.asyncio
async def test_engine_captures_stderr(tmp_path):
    run = Harness(_mode_profile("fail-exit")).create_run()
    await run.start("p", str(tmp_path))
    await asyncio.wait_for(_drain(run), timeout=3.0)
    await run.stop()
    assert "boom" in run.stderr_text  # fake CLI writes "boom" to stderr


@pytest.mark.asyncio
async def test_engine_stop_kills_hung_subprocess(tmp_path):
    # sleep-then ignores stdin close; stop() must escalate to SIGKILL and
    # still return within its bounded budget.
    run = Harness(_mode_profile("sleep-then", "30")).create_run()
    await run.start("p", str(tmp_path))
    await asyncio.wait_for(run.stop(), timeout=6.0)


@pytest.mark.skipif(
    os.name == "nt",
    reason="POSIX-only premise: the fallback dirs emulate a systemd service "
    "PATH (~/.local/bin, Homebrew, the POSIX nvm layout) and the probe is a "
    "#!/bin/sh script — Windows has no service PATH to strip and cannot exec a "
    "shebang script",
)
@pytest.mark.asyncio
async def test_engine_resolves_binary_from_fallback_dir(tmp_path, monkeypatch):
    """A bare binary not on PATH but in ~/.local/bin still resolves (the
    systemd case where the service PATH strips per-user dirs)."""
    fake_bin_dir = tmp_path / ".local" / "bin"
    fake_bin_dir.mkdir(parents=True)
    fake_binary = fake_bin_dir / "my-cli-xyzzy"
    fake_binary.write_text("#!/bin/sh\nexit 0\n")
    fake_binary.chmod(0o755)
    monkeypatch.setenv("PATH", "")
    monkeypatch.setenv("HOME", str(tmp_path))

    def build_turn_argv(ctx):
        return (["my-cli-xyzzy"], {"cwd": ctx.working_dir})

    profile = RuntimeProfile(**{**_stream_profile().__dict__, "build_turn_argv": build_turn_argv})
    run = Harness(profile).create_run()
    await run.start("p", str(tmp_path))  # resolves + spawns the trivial script
    await run.stop()


# --------------------------------------------------------------------------- #
# Shared assembly
# --------------------------------------------------------------------------- #


def test_callback_env_has_session_id_when_present():
    env = assembly.build_callback_env("sess-123")
    assert env["OCTOPUS_SESSION_ID"] == "sess-123"
    assert env["OCTOPUS_API_BASE"].startswith("http://127.0.0.1:")
    assert "OCTOPUS_AUTH_TOKEN" in env
    assert "OCTOPUS_SESSION_ID" not in assembly.build_callback_env(None)


def test_select_mcp_servers_all_by_default():
    env = assembly.build_callback_env("s")
    entries = assembly.select_mcp_servers(None, [], env)
    assert [e.key for e in entries] == ["bg", "ask", "ask_agent", "research"]
    bg = next(e for e in entries if e.key == "bg")
    assert bg.env["OCTOPUS_SESSION_ID"] == "s"
    ask_agent_entry = next(e for e in entries if e.key == "ask_agent")
    assert ask_agent_entry.env["OCTOPUS_SESSION_ID"] == "s"
    # `-P` keeps the child's cwd off `sys.path` (mcp_launch test file); the
    # module name still ends the argv so the harnesses render it unchanged.
    assert ask_agent_entry.args == ["-P", "-m", "server.mcp_servers.ask_agent"]


def test_select_mcp_servers_subset():
    env = assembly.build_callback_env("s")
    entries = assembly.select_mcp_servers(["ask"], [], env)
    assert [e.key for e in entries] == ["ask"]


def test_select_mcp_servers_silently_drops_unknown_legacy_names():
    # Existing agents may still carry "viewer" in their stored mcp_servers list
    # from before it became a client-only flow. Assembly should treat unknown
    # names as no-ops rather than failing, so old rows keep working.
    env = assembly.build_callback_env("s")
    entries = assembly.select_mcp_servers(["viewer", "bg"], [], env)
    assert [e.key for e in entries] == ["bg"]


def test_select_mcp_servers_merges_connectors():
    class _FakeConnector:
        def mcp_key(self, inst):
            return f"github_{inst}"

        def mcp_entry(self, inst, callback_env):
            return {"command": "py", "args": ["-m", "x"], "env": {**callback_env, "OCTOPUS_INSTALLATION_ID": inst}}

    env = assembly.build_callback_env("s")
    entries = assembly.select_mcp_servers(["bg"], [(_FakeConnector(), "abc123")], env)
    assert [e.key for e in entries] == ["bg", "github_abc123"]
    assert entries[1].env["OCTOPUS_INSTALLATION_ID"] == "abc123"


def test_compose_system_prompt_orders_persona_then_tools():
    assert assembly.compose_system_prompt(None, "TOOLS", []) == "TOOLS"
    assert assembly.compose_system_prompt("PERSONA", "TOOLS", []) == "PERSONA\n\nTOOLS"


# --------------------------------------------------------------------------- #
# Registry + derived predicates
# --------------------------------------------------------------------------- #


def test_registry_register_get_and_unknown():
    # A profile under a unique backend name so we don't collide with real ones.
    profile = RuntimeProfile(**{**_stream_profile().__dict__, "backend": "fake-test-backend"})
    harness = Harness(profile)
    register(harness)
    try:
        assert get_harness("fake-test-backend") is harness
        # None resolves to the default kind (which may be unregistered in
        # Phase 1) — unknown kinds raise explicitly.
        with pytest.raises(ValueError, match="Unknown backend"):
            get_harness("no-such-backend")
        # is_available()/available_backends reflect a resolvable binary
        # (sys.executable always resolves).
        assert harness.is_available() is True
        assert "fake-test-backend" in available_backends()
    finally:
        _REGISTRY.pop("fake-test-backend", None)


def test_derived_predicates_no_codec():
    h = Harness(_stream_profile())
    assert h.can_export is False
    assert h.can_import is False
    assert h.login is None
    assert h.premature_exit_recovery is False


# --------------------------------------------------------------------------- #
# run_oneshot
# --------------------------------------------------------------------------- #


def _oneshot_profile(result_line: str, *, mode: str = "emit-lines") -> RuntimeProfile:
    import json

    def build_oneshot_argv(ctx: OneShotContext) -> tuple[list[str], dict[str, Any]]:
        return ([sys.executable, str(FAKE_CLI), mode, result_line], {})

    def parse_oneshot_stdout(s: str) -> str:
        return json.loads(s.strip().splitlines()[-1]).get("result", "")

    return RuntimeProfile(
        **{
            **_stream_profile().__dict__,
            "build_oneshot_argv": build_oneshot_argv,
            "parse_oneshot_stdout": parse_oneshot_stdout,
        }
    )


@pytest.mark.asyncio
async def test_run_oneshot_returns_text():
    harness = Harness(_oneshot_profile('{"result":"hello world"}'))
    out = await harness.run_oneshot(OneShotContext(prompt="x"))
    assert out == "hello world"


@pytest.mark.asyncio
async def test_run_oneshot_empty_raises():
    harness = Harness(_oneshot_profile('{"result":""}'))
    with pytest.raises(HarnessOneshotError) as ei:
        await harness.run_oneshot(OneShotContext(prompt="x"))
    assert ei.value.code == "empty"


@pytest.mark.asyncio
async def test_run_oneshot_not_found_raises():
    def build_oneshot_argv(ctx):
        return (["definitely-not-a-real-binary-98765"], {})

    profile = RuntimeProfile(**{**_stream_profile().__dict__, "build_oneshot_argv": build_oneshot_argv})
    with pytest.raises(HarnessOneshotError) as ei:
        await Harness(profile).run_oneshot(OneShotContext(prompt="x"))
    assert ei.value.code == "not_found"


@pytest.mark.asyncio
async def test_run_oneshot_nonzero_exit_raises():
    harness = Harness(_oneshot_profile('{"result":"x"}', mode="fail-exit"))
    with pytest.raises(HarnessOneshotError) as ei:
        await harness.run_oneshot(OneShotContext(prompt="x"))
    assert ei.value.code == "failed"


# --------------------------------------------------------------- auth-error detection


def test_is_auth_error_claude_matches_401_and_phrases():
    """Claude/Anthropic 401 phrasings trip the real claude-code harness
    (harness-credential-reauth.md §3); benign output and empty text don't."""
    h = get_harness("claude-code")
    assert h.is_auth_error(
        "Failed to authenticate. API Error: 401 Invalid authentication credentials"
    )
    assert h.is_auth_error("oauth token has expired, please run /login")
    assert not h.is_auth_error("Tool returned HTTP 200; all good")
    assert not h.is_auth_error("")


def test_is_auth_error_codex_matches_and_is_case_insensitive():
    h = get_harness("codex")
    assert h.is_auth_error("stream error: 401 Unauthorized")
    assert h.is_auth_error("Your authentication token has expired")
    assert not h.is_auth_error("turn completed successfully")
    assert not h.is_auth_error("")


def test_is_stale_session_error_claude():
    """The CLI's wording when `--resume <id>` names a conversation it no longer
    has. Distinct from auth (the credential is fine) and from transient
    (retrying the same id fails forever)."""
    h = get_harness("claude-code")
    assert h.is_stale_session_error(
        "No conversation found with session ID: 7d06c77e-7541-43fe-a7cc-5ed5c98eca52"
    )
    assert not h.is_stale_session_error("API Error: 529 Overloaded")
    assert not h.is_stale_session_error("")
    # Codex declares no pattern, so it never claims this failure mode.
    assert not get_harness("codex").is_stale_session_error(
        "No conversation found with session ID: x"
    )


def test_is_auth_error_codex_matches_a_lapsed_chatgpt_login():
    """The verbatim `turn.failed` message a dead ChatGPT login produces. It
    used to match nothing, so the credential was never flagged and the user got
    a generic error instead of a Re-authorize prompt — it only *looked* handled
    because the CLI's stderr happens to carry a websocket "401 Unauthorized"
    alongside it (harness-credential-reauth.md §3)."""
    h = get_harness("codex")
    assert h.is_auth_error(
        "Your access token could not be refreshed. Please log out and sign in again."
    )
    # Each half stands on its own — the CLI words this differently by channel.
    assert h.is_auth_error("access token could not be refreshed")
    assert h.is_auth_error("Please log out and sign in again")


def test_is_auth_error_codex_ignores_bare_unauthorized_from_tools():
    """A non-auth failure that merely contains "unauthorized" (an MCP/connector
    401, a tool error) must NOT be read as a harness-credential failure — the
    patterns are auth-specific, never a bare "unauthorized" (Vera review)."""
    h = get_harness("codex")
    assert not h.is_auth_error("MCP server returned Unauthorized")
    assert not h.is_auth_error("tool failed: GitHub Unauthorized")
    assert not h.is_auth_error("your session token has expired, refetch it")


def test_is_transient_error_matches_server_reliability_failures():
    """5xx / overloaded / dropped-connection trip the retry classifier on both
    backends (harness-transient-retry.md §3)."""
    c, x = get_harness("claude-code"), get_harness("codex")
    assert c.is_transient_error("API Error: 529 Overloaded")
    assert c.is_transient_error("API Error: 503 Service Unavailable")
    assert c.is_transient_error("connection reset by peer")
    assert x.is_transient_error("stream error: 503 service unavailable")
    assert x.is_transient_error("error 500 internal server error")
    assert not c.is_transient_error("")
    assert not x.is_transient_error("turn completed")


def test_is_transient_error_excludes_quota_and_auth():
    """Quota/credit and auth failures must NOT be retried — they match no
    transient pattern (harness-transient-retry.md §2), keeping the three
    dispositions mutually exclusive."""
    c, x = get_harness("claude-code"), get_harness("codex")
    for blob in (
        "rate limit exceeded",
        "429 too many requests",
        "you have insufficient quota",
        "billing: credit balance is too low",
        "you have reached your usage limit",   # the USER's limit — not retryable
        "API Error: 401 Invalid authentication credentials",
        "invalid x-api-key",
    ):
        assert not c.is_transient_error(blob), blob
        assert not x.is_transient_error(blob), blob


def test_is_transient_error_retries_server_side_throttle():
    """The server-side throttle Anthropic marks "(not your usage limit)" IS a
    transient blip and must retry — even though it says "Rate limited". Keying
    on the specific phrasing distinguishes it from the user's own usage limit."""
    c = get_harness("claude-code")
    msg = (
        "API Error: Server is temporarily limiting requests "
        "(not your usage limit) · Rate limited"
    )
    assert c.is_transient_error(msg)


# --------------------------------------------------------------- process-group reaping
# turn-safety.md §2: turns spawn in their own process group so nested children
# (MCP servers / subagents) are reaped as a unit, not orphaned.


def test_prepare_spawn_isolates_the_process_group():
    """Which kwarg isolates a child's process group is the platform's business
    (`server/proc.py`); that a turn is isolated at all is not (turn-safety.md
    §2)."""
    from server.harness.run import prepare_spawn

    _, kwargs = prepare_spawn([sys.executable, "-c", "true"], {})
    if os.name == "nt":
        flags = kwargs.get("creationflags", 0)
        assert flags & subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        assert kwargs.get("start_new_session") is True


@pytest.mark.asyncio
async def test_kill_group_reaps_children():
    """Killing the group must take down a CHILD the spawned process started —
    the orphan leak a direct-child kill() leaves behind."""
    import server.proc as proc_mod
    from server.harness.run import prepare_spawn

    # Parent starts a grandchild, prints its pid, then waits — so the child
    # shares the parent's new process group. Python children rather than
    # `sh -c "sleep 30 &"`: no shell needed, so the test runs everywhere.
    script = (
        "import subprocess, sys, time\n"
        "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
        "print(p.pid, flush=True)\n"
        "time.sleep(30)\n"
    )
    argv, kwargs = prepare_spawn([sys.executable, "-c", script], {})
    proc = await asyncio.create_subprocess_exec(
        *argv, stdout=asyncio.subprocess.PIPE, **kwargs
    )
    child_pid = int((await proc.stdout.readline()).strip())
    assert proc_mod.pid_alive(child_pid)

    sent_group = proc_mod.kill_group(proc)
    await proc.wait()
    for _ in range(100):  # let the OS finish reaping the child
        if not proc_mod.pid_alive(child_pid):
            break
        await asyncio.sleep(0.05)
    assert sent_group is True
    assert not proc_mod.pid_alive(child_pid), "child process was orphaned, not reaped"


@pytest.mark.asyncio
async def test_run_oneshot_reaps_group_on_cancel(monkeypatch):
    """Cancelling a run_oneshot mid-flight must reap its process group, not
    orphan the CLI (Vera review). We spy on the group-kill helper."""
    import server.harness.harness as harness_mod

    calls: list[str] = []
    real = harness_mod.kill_group

    def spy(proc):
        calls.append("kill")
        return real(proc)

    monkeypatch.setattr(harness_mod, "kill_group", spy)

    def build_oneshot_argv(ctx):
        return ([sys.executable, "-c", "import time; time.sleep(30)"], {})

    profile = RuntimeProfile(
        **{**_stream_profile().__dict__, "build_oneshot_argv": build_oneshot_argv}
    )
    task = asyncio.create_task(
        Harness(profile).run_oneshot(OneShotContext(prompt="x"), timeout=30)
    )
    await asyncio.sleep(0.4)  # let the subprocess spawn
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert calls == ["kill"]


# --------------------------------------------------------------------------- #
# stdin as an input channel (inline-steering.md §6)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_stream_json_writes_the_prompt_to_stdin(tmp_path):
    """Under STREAM_JSON the prompt is a frame on stdin, not an argv tail."""
    profile = _mode_profile("echo-stdin")
    run = Harness(profile).create_run()
    await run.start("hello from stdin", str(tmp_path))
    events = await asyncio.wait_for(_drain(run), timeout=5.0)
    await run.stop()

    seen = [e.raw for e in events if e.raw and e.raw.get("type") == "frame_seen"]
    assert [f["content"] for f in seen] == ["hello from stdin"]
    # And the argv carries no prompt.
    argv, _ = run.build_argv("hello from stdin", str(tmp_path))
    assert "hello from stdin" not in argv


@pytest.mark.asyncio
async def test_every_frame_uuid_is_distinct(tmp_path):
    """The real CLI reports our frame uuid back as `command_uuid` and
    deduplicates on it: two frames sharing a uuid means the second command is
    silently dropped and the turn hangs waiting for a reply that never comes.

    This cost a real debugging session — a uuid derived from
    (session_id, turn_index) repeated on every turn of a session, so the second
    turn of any resumed session hung for the full timeout.
    """
    profile = _mode_profile("echo-stdin")
    run = Harness(profile).create_run()
    await run.start("first", str(tmp_path))
    second = await run.send_user_frame("second")
    third = await run.send_user_frame("third")

    assert run._initial_uuid is not None
    assert len({run._initial_uuid, second, third}) == 3
    await run.stop()


@pytest.mark.asyncio
async def test_send_user_frame_refused_on_argv_backends(tmp_path):
    """A backend whose prompt lives in argv has no input channel; asking it to
    take one must fail loudly rather than write into a closed pipe."""
    profile = _stream_profile(
        '{"type":"result"}', stdin_mode=StdinMode.CLOSE_AFTER_SPAWN
    )
    run = Harness(profile).create_run()
    await run.start("p", str(tmp_path))
    with pytest.raises(RuntimeError, match="does not take input on stdin"):
        await run.send_user_frame("late")
    await run.stop()


# --------------------------------------------------------------------------- #
# Reuse identity (inline-steering.md §7)
# --------------------------------------------------------------------------- #


def _sig_profile() -> RuntimeProfile:
    return _stream_profile('{"type":"result"}')


def test_spawn_signature_is_stable_across_turns():
    """The signature decides whether a live process may serve the next turn, so
    it must depend on CONFIG, never on object identity.

    `_load_connectors` builds fresh connector objects every turn. Keying on the
    object meant its default repr — which carries a memory address — landed in
    the signature, so every turn looked like a config change and reuse silently
    never happened. That fails quietly, as a permanent ~1s-per-turn tax.
    """
    class _Conn:
        kind = "gmail"

    class _Inst:
        id = "install-1"

    profile = _sig_profile()
    cfg = dict(system_prompt="P", model="m")
    a = Harness(profile).create_run(RunConfig(connectors=[(_Conn(), _Inst())], **cfg))
    b = Harness(profile).create_run(RunConfig(connectors=[(_Conn(), _Inst())], **cfg))
    assert a.spawn_signature("/tmp", None) == b.spawn_signature("/tmp", None)


def test_spawn_signature_changes_with_anything_baked_in_at_spawn():
    """Persona, model, tool policy, MCP set and working dir are all argv or env
    at spawn and cannot change afterwards — each must force a respawn."""
    profile = _sig_profile()
    base = dict(system_prompt="P", model="m", mcp_servers=["bg"],
                tool_allow=["Read"], tool_deny=["Write"], memory_dir="/m")
    ref = Harness(profile).create_run(RunConfig(**base)).spawn_signature("/tmp", None)

    for field, value in [
        ("system_prompt", "DIFFERENT"),
        ("model", "other-model"),
        ("mcp_servers", ["bg", "ask"]),
        ("tool_allow", ["Read", "Glob"]),
        ("tool_deny", []),
        ("memory_dir", "/other"),
    ]:
        changed = {**base, field: value}
        got = Harness(profile).create_run(RunConfig(**changed)).spawn_signature("/tmp", None)
        assert got != ref, f"{field} must change the signature"

    # …and so must the working dir.
    same_cfg = Harness(profile).create_run(RunConfig(**base))
    assert same_cfg.spawn_signature("/elsewhere", None) != ref


# --------------------------------------------------------------------------- #
# stdin as a request/response protocol (dsh-harness.md §3.1)
# --------------------------------------------------------------------------- #

JSONRPC_CLI = Path(__file__).parent / "_fixtures" / "fake_jsonrpc_cli.py"


class _NotificationParser(EventParser):
    """Maps a runtime notification onto one event, the way a protocol's own
    EventParser maps its runtime's notifications."""

    def parse(self, obj: dict[str, Any]) -> ParseOutput:
        if obj.get("method") == "session/update":
            return ParseOutput(
                events=[HarnessEvent(type="update", raw=obj.get("params"))]
            )
        return ParseOutput()


class _ScriptedProtocol(TerminalProtocol):
    """A minimal protocol: a two-call handshake, one prompt frame per turn, and
    an answer for whatever the runtime asks back.

    Deliberately not ACP-shaped — this exercises the *engine's* plumbing
    (routing, correlation, answering), not any one runtime's semantics.
    """

    def __init__(self) -> None:
        self._next_id = 1
        self._turn_id: Any = None
        self.answered: list[tuple[str, dict[str, Any]]] = []

    def _id(self) -> int:
        self._next_id += 1
        return self._next_id

    async def handshake(self, run, ctx):
        await run.request(
            {"jsonrpc": "2.0", "id": self._id(), "method": "initialize", "params": {}}
        )
        result = await run.request(
            {
                "jsonrpc": "2.0",
                "id": self._id(),
                "method": "session/new",
                "params": {"cwd": ctx.working_dir},
            }
        )
        return result.get("sessionId")

    async def send_turn(self, run, text):
        self._turn_id = self._id()
        await run.write_frame(
            {
                "jsonrpc": "2.0",
                "id": self._turn_id,
                "method": "session/prompt",
                "params": {"text": text},
            }
        )
        return str(self._turn_id)

    def classify(self, obj):
        if "method" in obj:
            return FrameKind.REQUEST if "id" in obj else FrameKind.EVENT
        return FrameKind.RESPONSE

    async def on_response(self, run, request_id, result, error):
        if request_id == self._turn_id:
            self._turn_id = None
            return ParseOutput(
                events=[HarnessEvent(type="result", raw=result)], end_of_stream=True
            )
        return ParseOutput()

    async def on_request(self, run, request_id, method, params):
        self.answered.append((method, params))
        return {"jsonrpc": "2.0", "id": request_id, "result": {"allow": True}}


def _protocol_profile(
    mode: str = "session", protocol: TerminalProtocol | None = None
) -> RuntimeProfile:
    def build_turn_argv(ctx: TurnContext) -> tuple[list[str], dict[str, Any]]:
        return ([sys.executable, str(JSONRPC_CLI), mode], {"cwd": ctx.working_dir})

    # A fresh protocol per run, the same way `new_event_parser` works: a
    # profile is shared by every run of its kind, so the per-run state (the
    # turn in flight, the request being awaited) must not live on it.
    def new_protocol() -> TerminalProtocol:
        return protocol if protocol is not None else _ScriptedProtocol()

    return RuntimeProfile(
        backend="fake-protocol",
        binary=sys.executable,
        tools_prompt="TOOLS",
        credential_style="env_secret",
        premature_exit_recovery=False,
        stdin_mode=StdinMode.PROTOCOL,
        new_protocol=new_protocol,
        build_turn_argv=build_turn_argv,
        new_event_parser=_NotificationParser,
        build_oneshot_argv=lambda ctx: ([sys.executable], {}),
        parse_oneshot_stdout=lambda s: s,
    )


@pytest.mark.asyncio
async def test_protocol_handshake_publishes_the_session_id_and_routes_frames(tmp_path):
    """A PROTOCOL run: the handshake yields the engine-side conversation id
    (published as `session_started`), the three frame kinds are routed apart,
    the runtime's own request is answered rather than ignored, and the turn's
    *response* is what settles the stream.
    """
    protocol = _ScriptedProtocol()
    run = Harness(_protocol_profile(protocol=protocol)).create_run()
    await run.start("hi", str(tmp_path))
    events = await asyncio.wait_for(_drain(run), timeout=5.0)
    await run.stop()

    assert [e.type for e in events] == ["session_started", "update", "update", "result"]
    assert events[0].session_id == "sess-1"
    # The first update is the runtime's own; the second only exists because the
    # client answered, so its payload proves the answer reached the CLI.
    assert events[1].raw == {"seq": 1, "text": "hi"}
    assert events[2].raw == {"seq": 2, "answer": {"allow": True}}
    assert protocol.answered == [("ask", {"q": "?"})]


@pytest.mark.asyncio
async def test_protocol_error_response_surfaces_with_its_wire_code(tmp_path):
    """A JSON-RPC error is a failed request, and its code must survive — the
    classifications downstream (auth / stale / transient) key on it."""
    run = Harness(_protocol_profile("error")).create_run()
    with pytest.raises(ProtocolRequestError) as exc:
        await run.start("hi", str(tmp_path))
    assert exc.value.code == -32000
    await run.stop()


@pytest.mark.asyncio
async def test_protocol_dead_process_fails_a_pending_request(tmp_path):
    """A CLI that exits before answering must fail the turn loudly instead of
    leaving it to wait for a reply the watchdog would eventually have to kill.
    """
    run = Harness(_protocol_profile("die")).create_run()
    with pytest.raises(ProtocolRequestError):
        await asyncio.wait_for(run.start("hi", str(tmp_path)), timeout=5.0)
    await run.stop()


@pytest.mark.asyncio
async def test_protocol_reuses_the_process_for_the_next_turn(tmp_path):
    """A protocol run serves several turns from one process: the second turn
    goes out as another prompt frame, not a respawn."""
    protocol = _ScriptedProtocol()
    run = Harness(_protocol_profile(protocol=protocol)).create_run()
    await run.start("first", str(tmp_path))
    await asyncio.wait_for(_drain(run), timeout=5.0)

    sent: list[str] = []
    original = protocol.send_turn

    async def spy(r, text):
        sent.append(text)
        return await original(r, text)

    protocol.send_turn = spy  # type: ignore[method-assign]
    await run.send_turn("second")
    events = await asyncio.wait_for(_drain(run), timeout=5.0)
    await run.stop()

    assert sent == ["second"]
    assert [e.type for e in events] == ["update", "update", "result"]


def test_protocol_is_reusable_but_not_steerable():
    """Process reuse and mid-turn steering are different capabilities: a
    protocol run takes turns one at a time, so a message sent mid-flight must
    queue instead of being written into a conversation that isn't reading."""
    run = Harness(_protocol_profile()).create_run()
    assert run.reusable is True
    assert run.can_steer is False


def test_stream_json_is_reusable_and_steerable():
    run = Harness(_stream_profile('{"type":"result"}')).create_run()
    assert run.reusable is True
    assert run.can_steer is True


def test_argv_backend_is_neither_reusable_nor_steerable():
    run = Harness(
        _stream_profile('{"type":"result"}', stdin_mode=StdinMode.CLOSE_AFTER_SPAWN)
    ).create_run()
    assert run.reusable is False
    assert run.can_steer is False


@pytest.mark.asyncio
async def test_protocol_without_a_collaborator_fails_loudly(tmp_path):
    """A PROTOCOL profile with no protocol is a programming error: falling back
    to a raw user frame would send a prompt the CLI cannot read."""
    profile = _protocol_profile()
    broken = RuntimeProfile(**{**profile.__dict__, "new_protocol": None})
    run = Harness(broken).create_run()
    with pytest.raises(RuntimeError, match="without a TerminalProtocol"):
        await run.start("hi", str(tmp_path))
    await run.stop()
