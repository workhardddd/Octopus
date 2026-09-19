"""DSH harness tests (docs/plans/dsh-harness.md §3).

Two layers: the profile's own rendering (argv, environment, declared
degradations) as pure functions, and the ACP client driving a scripted
`dsh --profile acp` over real pipes — so the handshake, the frame routing, the
event mapping, the chunk coalescing and the permission answer are exercised
without the real CLI or a DeepSeek key.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from server.harness import (
    Harness,
    HarnessCredential,
    OneShotContext,
    RunConfig,
    RuntimeProfile,
    StdinMode,
    TurnContext,
    get_harness,
)
from server.harness import assembly
from server.harness.dsh import (
    ACP_PROFILE,
    HEADLESS_PROFILE,
    build_oneshot_argv,
    build_turn_argv,
    parse_oneshot_stdout,
)
from server.harness.dsh_acp import DshAcpProtocol, DshEventParser, model_option_value
from server.harness.profile import McpServerEntry

FAKE_ACP = Path(__file__).parent / "_fixtures" / "fake_dsh_acp.py"
PATCH = "/tmp/dsh-home/patch.yml"


# --------------------------------------------------------------------------- #
# The profile's own rendering
# --------------------------------------------------------------------------- #


def _ctx(**overrides: Any) -> TurnContext:
    base: dict[str, Any] = dict(
        prompt="hi",
        working_dir="/tmp/wd",
        resume_id=None,
        system_prompt="PERSONA",
        model=None,
        tool_allow=None,
        tool_deny=None,
        mcp_servers=[],
        credential=None,
        dsh_home="/tmp/dsh-home",
        dsh_patch=PATCH,
    )
    base.update(overrides)
    return TurnContext(**base)


def test_dsh_turn_renders_the_acp_profile_with_its_patch():
    argv, kwargs = build_turn_argv(_ctx())
    assert argv == ["dsh", "--profile", ACP_PROFILE, "--patch", PATCH]
    assert kwargs["cwd"] == "/tmp/wd"
    # The prompt is never in argv: it is a `session/prompt` request.
    assert "hi" not in argv


def test_dsh_turn_exports_its_home_and_injects_the_credential():
    """`DSH_HOME` must be exported (DSH rejects `DSH_*` from a `.env` file) and
    the key rides the launch environment, which outranks every credentials file
    DSH reads — so the agent's home never holds a copy of it at rest."""
    credential = HarnessCredential(backend="dsh", auth_type="api_key", secret="sk-test")
    _, kwargs = build_turn_argv(_ctx(credential=credential))
    assert kwargs["env"]["DSH_HOME"] == "/tmp/dsh-home"
    assert kwargs["env"]["DEEPSEEK_API_KEY"] == "sk-test"


def test_dsh_turn_refuses_to_run_without_a_home_or_a_patch():
    """A turn without a home would write into the user's own `~/.dsh`; a turn
    without the generated patch would carry no persona and — worse — no
    permission posture, and an unanswered approval request has no timeout on
    the ACP path."""
    with pytest.raises(ValueError, match="DSH_HOME"):
        build_turn_argv(_ctx(dsh_home=None))
    with pytest.raises(ValueError, match="patch"):
        build_turn_argv(_ctx(dsh_patch=None))


def test_dsh_oneshot_renders_the_headless_profile():
    argv, kwargs = build_oneshot_argv(
        OneShotContext(prompt="every weekday at 9", dsh_home="/tmp/dsh-home")
    )
    assert argv == ["dsh", "--profile", HEADLESS_PROFILE, "every weekday at 9"]
    assert kwargs["env"]["DSH_HOME"] == "/tmp/dsh-home"
    # No patch, no working dir: a one-shot needs neither, and `headless` has no
    # blocking approval channel to be trapped by.
    assert "--patch" not in argv
    assert parse_oneshot_stdout("  the answer\n") == "the answer"


def test_dsh_profile_declares_its_degradations():
    """Everything DSH cannot do is a declared property, never a silent
    fallback (docs/plans/dsh-harness.md §10)."""
    profile = get_harness("dsh").profile
    assert profile.stdin_mode is StdinMode.PROTOCOL
    assert profile.new_protocol is DshAcpProtocol
    assert profile.login is None  # an API key, pasted — no login flow
    assert profile.transcript_codec is None  # no handoff/pull
    assert not profile.injects_memory_prompt  # DSH reads the dir natively
    assert not profile.premature_exit_recovery  # a Claude-CLI quirk
    assert profile.can_fork and profile.fork_copy is None  # replay-only forks
    assert profile.web is not None
    assert profile.web.tool_names == ("web_search", "web_fetch")


def test_dsh_run_is_reusable_but_not_steerable():
    run = Harness(get_harness("dsh").profile).create_run()
    assert run.reusable is True  # one process serves the session's turns
    assert run.can_steer is False  # ACP has no mid-turn input channel


def test_model_option_value_is_a_json_stringify_pair():
    """ACP's `model` value is opaque to the protocol and IS
    `JSON.stringify([provider, model])` on the wire, so the driver composes
    exactly that — a bare model id belongs to the default route."""
    assert model_option_value("deepseek-flash") == (
        '["deepseek-official","deepseek-flash"]'
    )
    assert model_option_value("acme/think") == '["acme","think"]'


# --------------------------------------------------------------------------- #
# The ACP client, over real pipes
# --------------------------------------------------------------------------- #


def _acp_profile(mode: str, record: Path) -> RuntimeProfile:
    """The real protocol and parser, with the fake CLI standing in for `dsh`.
    The argv builder is the only thing replaced — everything else under test is
    production code.
    """

    def build(ctx: TurnContext) -> tuple[list[str], dict[str, Any]]:
        return (
            [sys.executable, str(FAKE_ACP), str(record), mode],
            {"cwd": ctx.working_dir},
        )

    return RuntimeProfile(
        backend="dsh",
        binary=sys.executable,
        tools_prompt="TOOLS",
        credential_style="env_secret",
        premature_exit_recovery=False,
        stdin_mode=StdinMode.PROTOCOL,
        new_protocol=DshAcpProtocol,
        build_turn_argv=build,
        new_event_parser=DshEventParser,
        build_oneshot_argv=lambda ctx: ([sys.executable], {}),
        parse_oneshot_stdout=lambda s: s,
    )


def _run_config(tmp_path: Path, **overrides: Any) -> RunConfig:
    base: dict[str, Any] = dict(
        dsh_home=str(tmp_path / "home"),
        dsh_patch=str(tmp_path / "patch.yml"),
    )
    base.update(overrides)
    return RunConfig(**base)


def _frames(record: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in record.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


async def _drain(run) -> list[Any]:
    return [event async for event in run.stream()]


@pytest.mark.asyncio
async def test_dsh_acp_drives_a_full_turn(tmp_path, monkeypatch):
    """One turn end to end: handshake, per-session MCP, the opaque model
    value, committed text/thoughts/tool lifecycle, a permission request the
    client answers, and the prompt's settlement ending the stream."""
    record = tmp_path / "frames.jsonl"
    run = Harness(_acp_profile("turn", record)).create_run(
        _run_config(tmp_path, model="deepseek-flash", mcp_servers=["bg"])
    )
    monkeypatch.setattr(
        assembly,
        "select_mcp_servers",
        lambda *a, **k: [
            McpServerEntry(
                key="bg", command="/usr/bin/python", args=["bg.py"], env={"X": "1"}
            )
        ],
    )

    await run.start("hi", str(tmp_path))
    events = await asyncio.wait_for(_drain(run), timeout=10.0)
    await run.stop()

    assert events[0].type == "session_started"
    assert events[0].session_id == "dsh-sess-1"
    # Committed chunks of ONE message coalesce into a single text event.
    assert [e.type for e in events[1:]] == [
        "thinking",
        "text",
        "tool_use",
        "tool_result",
        "result",
    ]
    assert events[2].content == "Hello world"
    assert events[3].tool_use_id == "tc-1"
    assert events[3].tool_name == "read"
    assert events[4].tool_use_id == "tc-1"
    assert events[4].content == "file body"
    assert events[4].is_error is False
    assert events[5].session_id == "dsh-sess-1"

    sent = _frames(record)
    methods = [f["method"] for f in sent if "method" in f]
    assert methods == [
        "initialize",
        "session/new",
        "session/set_config_option",
        "session/prompt",
    ]
    new_session = next(f for f in sent if f.get("method") == "session/new")
    assert new_session["params"]["cwd"] == str(tmp_path)
    assert new_session["params"]["mcpServers"] == [
        {
            "name": "bg",
            "command": "/usr/bin/python",
            "args": ["bg.py"],
            "env": [{"name": "X", "value": "1"}],
        }
    ]
    config = next(f for f in sent if f.get("method") == "session/set_config_option")
    assert config["params"]["configId"] == "model"
    assert config["params"]["value"] == '["deepseek-official","deepseek-flash"]'
    # The permission request was answered (and declined) before settlement.
    answer = sent[-1]
    assert "method" not in answer
    assert answer["id"] == "perm-1"
    assert answer["result"] == {
        "outcome": {"outcome": "selected", "optionId": "reject-once"}
    }


@pytest.mark.asyncio
async def test_dsh_acp_resume_resends_mcp_servers(tmp_path, monkeypatch):
    """A resume without `mcpServers` composes a session with no MCP at all, so
    the declarations must be re-sent on every resume."""
    record = tmp_path / "frames.jsonl"
    run = Harness(_acp_profile("turn", record)).create_run(
        _run_config(tmp_path, mcp_servers=["bg"])
    )
    monkeypatch.setattr(assembly, "select_mcp_servers", lambda *a, **k: [])

    await run.start("hi", str(tmp_path), resume_id="dsh-sess-old")
    await asyncio.wait_for(_drain(run), timeout=10.0)
    await run.stop()

    sent = _frames(record)
    resumed = next(f for f in sent if f.get("method") == "session/resume")
    assert resumed["params"]["sessionId"] == "dsh-sess-old"
    assert resumed["params"]["mcpServers"] == []
    assert not any(f.get("method") == "session/new" for f in sent)
    # The route is selected on every turn, naming the default when the agent
    # names no model — DSH's shipped ACP config pins one nobody chose, and the
    # selection is what decides the image gate (server/dsh_home.py).
    config = next(f for f in sent if f.get("method") == "session/set_config_option")
    assert config["params"]["value"] == '["deepseek-official","deepseek-flash"]'


@pytest.mark.asyncio
async def test_dsh_acp_error_response_settles_the_turn_as_an_error(tmp_path):
    record = tmp_path / "frames.jsonl"
    run = Harness(_acp_profile("error", record)).create_run(_run_config(tmp_path))
    await run.start("hi", str(tmp_path))
    events = await asyncio.wait_for(_drain(run), timeout=10.0)
    await run.stop()

    assert events[-1].type == "error"
    assert events[-1].is_error is True
    # The wire code survives: the auth/stale/transient classifiers key on it.
    assert "-32001" in (events[-1].content or "")


@pytest.mark.asyncio
async def test_dsh_acp_cancel_asks_the_runtime_to_cancel(tmp_path):
    """Interrupting must go through ACP's own cancellation (`session/cancel`)
    before the process is killed: the runtime then settles the turn instead of
    being shot mid-flight."""
    record = tmp_path / "frames.jsonl"
    run = Harness(_acp_profile("slow", record)).create_run(_run_config(tmp_path))
    await run.start("hi", str(tmp_path))

    received: list[Any] = []

    async def consume() -> None:
        async for event in run.stream():
            received.append(event)

    task = asyncio.create_task(consume())
    for _ in range(400):  # wait for the first committed update
        if received:
            break
        await asyncio.sleep(0.01)
    assert received, "the fake CLI never emitted its update"

    await run.interrupt()
    await asyncio.wait_for(task, timeout=5.0)

    assert any(f.get("method") == "session/cancel" for f in _frames(record))
    assert received[-1].type == "result"


# --------------------------------------------------------------------------- #
# The parser, in isolation
# --------------------------------------------------------------------------- #


def _update(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "method": "session/update",
        "params": {"sessionId": "s", "update": payload},
    }


def test_parser_ignores_non_terminal_tool_progress_and_other_updates():
    """A progress update emits nothing (the card is written once by the
    `tool_call`), and updates the transcript has no vocabulary for are dropped
    rather than rendered as something invented."""
    parser = DshEventParser()
    progress = _update(
        {"sessionUpdate": "tool_call_update", "toolCallId": "t", "status": "in_progress"}
    )
    assert parser.parse(progress).events == []
    assert parser.parse(_update({"sessionUpdate": "usage_update"})).events == []
    assert parser.parse({"jsonrpc": "2.0", "id": 1, "result": {}}).events == []


def test_parser_emits_a_tool_call_that_arrives_already_finished():
    """A `tool_call` with a terminal status carries its own result, so both
    events come from the one frame."""
    parser = DshEventParser()
    out = parser.parse(
        _update(
            {
                "sessionUpdate": "tool_call",
                "toolCallId": "t1",
                "title": "Read a.txt",
                "name": "read",
                "status": "completed",
                "rawInput": {"path": "a.txt"},
                "rawOutput": "body",
            }
        )
    )
    assert [e.type for e in out.events] == ["tool_use", "tool_result"]
    assert out.events[1].content == "body"


def test_parser_splits_messages_when_the_message_id_changes():
    """Chunks tagged with a new `messageId` start a new message, so the
    previous one is flushed as its own text event."""
    parser = DshEventParser()
    first = _update(
        {
            "sessionUpdate": "agent_message_chunk",
            "messageId": "m1",
            "content": {"type": "text", "text": "one"},
        }
    )
    second = _update(
        {
            "sessionUpdate": "agent_message_chunk",
            "messageId": "m2",
            "content": {"type": "text", "text": "two"},
        }
    )
    assert parser.parse(first).events == []
    out = parser.parse(second)
    assert [e.content for e in out.events] == ["one"]
    assert [e.content for e in parser.flush().events] == ["two"]


def test_parser_drops_non_text_content():
    parser = DshEventParser()
    image = _update(
        {
            "sessionUpdate": "agent_message_chunk",
            "content": {"type": "image", "data": "AAAA", "mimeType": "image/png"},
        }
    )
    assert parser.parse(image).events == []
    assert parser.flush().events == []
