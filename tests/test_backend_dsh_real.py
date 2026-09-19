"""End-to-end DSH tests against the real `dsh` binary and a real API key.

Gated on `dsh` on PATH **and** `DEEPSEEK_API_KEY` in the environment: DSH
authenticates with a key rather than a login flow, so without one a turn cannot
run and these tests would fail in a way that looks like a broken harness
(tests/cli_gate.py holds the gate, and says why it is shallow).

They sit alongside tests/test_harness_dsh.py (a scripted fake ACP server): the
fake tests are the fast regression net for the client and the parser, these
prove the wire really is what `dsh --profile acp` speaks, that a generated patch
and a per-agent home produce a working session, and — the one that matters most
— that a rejected credential is classified as one.

Each test gets its own `DSH_HOME` under a temp root, so nothing here touches
whatever the person running the tests uses DSH for.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from server import dsh_home
from server.config import settings
from server.harness import (
    Harness,
    HarnessCredential,
    OneShotContext,
    RunConfig,
    get_harness,
)
from tests.cli_gate import dsh_cli_present, dsh_cli_works

requires_key = pytest.mark.skipif(
    not dsh_cli_works(),
    reason="dsh not on PATH or DEEPSEEK_API_KEY unset; skipping real DSH turns",
)
requires_cli = pytest.mark.skipif(
    not dsh_cli_present(),
    reason="dsh CLI not on PATH; skipping real-CLI tests",
)

#: Generous: a cold home materializes DSH's profile workspace on first spawn,
#: which is slower than a turn and, on a cold cache, needs the network.
FIRST_TURN_TIMEOUT = 420.0
TURN_TIMEOUT = 240.0


@pytest.fixture
def dsh_env(tmp_path, monkeypatch):
    """An isolated DSH home for one test, plus the agent id it belongs to."""
    monkeypatch.setattr(settings, "dsh_home_dir", str(tmp_path / "dsh"))
    monkeypatch.setattr(settings, "agents_dir", str(tmp_path / "agents"))
    return "agent-real"


def _key() -> str:
    key = os.environ.get("DEEPSEEK_API_KEY", "")
    assert key, "the gate said the key is present"
    return key


def _credential(secret: str | None = None) -> HarnessCredential:
    return HarnessCredential(
        backend="dsh", auth_type="api_key", secret=secret or _key()
    )


def _run(agent_id: str, **config) -> Harness:
    """A DSH run for one turn. The credential is *not* part of RunConfig — it
    arrives per invocation on `start()` (see `_start`)."""
    base = dict(agent_id=agent_id, system_prompt="You are a terse test agent.")
    base.update(config)
    return Harness(get_harness("dsh").profile).create_run(RunConfig(**base))


async def _start(
    run: Harness,
    prompt: str,
    working_dir: str,
    resume_id: str | None = None,
    credential: HarnessCredential | None = None,
) -> None:
    await run.start(
        prompt,
        working_dir,
        resume_id,
        credential=credential if credential is not None else _credential(),
    )


async def _drain(run, timeout: float) -> list:
    events: list = []

    async def collect() -> None:
        async for event in run.stream():
            events.append(event)

    try:
        await asyncio.wait_for(collect(), timeout=timeout)
    except asyncio.TimeoutError:
        raise AssertionError(
            f"the turn did not settle within {timeout}s. "
            f"Collected: {[e.type for e in events]}\nstderr: {run.stderr_text[:800]}"
        )
    return events


def _text_of(events: list) -> str:
    return "\n".join(e.content or "" for e in events if e.type == "text")


@requires_key
@pytest.mark.asyncio
async def test_a_real_turn_streams_text_and_settles(tmp_path, dsh_env):
    """One real turn: the handshake yields a session, the model answers, and the
    prompt's settlement ends the stream."""
    run = _run(dsh_env)
    await _start(run, "Reply with exactly the word: PONG", str(tmp_path))
    events = await _drain(run, FIRST_TURN_TIMEOUT)
    await run.stop()

    types = [e.type for e in events]
    assert types[0] == "session_started", types
    assert events[0].session_id, "the handshake must publish DSH's session id"
    assert "result" in types, f"no settlement; got {types}\n{run.stderr_text[:800]}"
    assert not [e for e in events if e.type == "error"], _text_of(events)
    assert "PONG" in _text_of(events).upper(), _text_of(events)


@requires_key
@pytest.mark.asyncio
async def test_a_real_turn_reuses_its_process_for_the_next_turn(tmp_path, dsh_env):
    """A held ACP process takes a second prompt: that is the whole reason
    `reusable` is true for a protocol backend (dsh-harness.md §3.6)."""
    run = _run(dsh_env)
    await _start(run, "Reply with exactly the word: PONG", str(tmp_path))
    await _drain(run, FIRST_TURN_TIMEOUT)

    await run.send_turn("Now reply with exactly the word: SECOND")
    events = await _drain(run, TURN_TIMEOUT)
    await run.stop()

    assert "SECOND" in _text_of(events).upper(), _text_of(events)


@requires_key
@pytest.mark.asyncio
async def test_a_real_session_resumes_in_a_fresh_process(tmp_path, dsh_env):
    """DSH persists sessions and `session/resume` reconnects them, so a session
    survives Octopus restarting the process — the resume id is durable state,
    not a live-process handle (dsh-harness.md §0.1.3, §3.6)."""
    first = _run(dsh_env)
    await _start(first, "Remember this word: PLATYPUS", str(tmp_path))
    events = await _drain(first, FIRST_TURN_TIMEOUT)
    await first.stop()
    session_id = next(e.session_id for e in events if e.type == "session_started")
    assert session_id

    second = _run(dsh_env)
    await _start(
        second,
        "What word did I ask you to remember? Answer with that word only.",
        str(tmp_path),
        resume_id=session_id,
    )
    resumed = await _drain(second, TURN_TIMEOUT)
    await second.stop()

    assert any(e.type == "session_started" and e.session_id for e in resumed)
    assert "PLATYPUS" in _text_of(resumed).upper(), _text_of(resumed)


@requires_key
@pytest.mark.asyncio
async def test_a_real_oneshot_returns_the_answer(tmp_path, dsh_env):
    """`/schedule` parsing and the research reasoning leaf both go through
    `run_oneshot`, which is `dsh --profile headless` (dsh-harness.md §3.7)."""
    harness = get_harness("dsh")
    text = await harness.run_oneshot(
        OneShotContext(
            prompt="Reply with exactly: ONESHOT",
            agent_id=dsh_env,
            credential=_credential(),
        ),
        timeout=FIRST_TURN_TIMEOUT,
    )
    assert "ONESHOT" in text.upper(), text


@requires_cli
@pytest.mark.asyncio
async def test_a_rejected_key_is_classified_as_an_auth_error(tmp_path, dsh_env):
    """The pattern tables in `dsh.py` are the only thing that turns a failed
    turn into "re-authenticate this credential" — and a guessed table would
    silently make that feature a no-op, so it is exercised against a real
    rejection rather than assumed."""
    run = _run(dsh_env)
    await _start(
        run,
        "Reply with exactly the word: PONG",
        str(tmp_path),
        credential=_credential("sk-not-a-real-deepseek-key-000000000000"),
    )
    events = await _drain(run, TURN_TIMEOUT)
    stderr = run.stderr_text
    await run.stop()

    combined = "\n".join(
        [e.content or "" for e in events] + [stderr]
    )
    assert not any(
        e.type == "result" for e in events
    ), f"a rejected key must not settle as a successful turn: {combined[:800]}"
    assert get_harness("dsh").is_auth_error(combined), (
        "a rejected credential was not recognised as one. The real error text "
        "was:\n" + combined[:1500] + "\n\nUpdate _DSH_AUTH_ERROR_PATTERNS in "
        "server/harness/dsh.py to match it."
    )


@requires_key
@pytest.mark.asyncio
async def test_a_real_web_leaf_runs_scoped(tmp_path, dsh_env):
    """The research web leaf is a full turn under a *scoped* patch
    (dsh-harness.md §3.7): no memory, and the shell/filesystem/sub-agent rows
    turned off while the web tools stay on. ACP cannot set a per-turn tool
    policy, so that composition is the only thing standing between a leaf and
    the host — and the conformance test proves only that it *composes*, not that
    it runs. This runs it, twice: once to prove a leaf can answer at all, and
    once to prove the shell really is gone."""
    from server.research.leaf import run_web_leaf

    harness = get_harness("dsh")
    answered = await run_web_leaf(
        harness,
        prompt=(
            "Search the web for the capital of Australia, then reply with "
            "exactly one line: RESULT=<city>."
        ),
        working_dir=str(tmp_path),
        credential=_credential(),
        model=None,
        agent_id=dsh_env,
        timeout=FIRST_TURN_TIMEOUT,
    )
    assert answered.error is None, answered.error
    assert answered.text.strip(), "a web leaf must return text"

    # Scoping, tested by its teeth: a leaf has no shell to leak through.
    scoped = await run_web_leaf(
        harness,
        prompt=(
            "Use your shell tool to run `echo LEAKED` and report its output "
            "verbatim. If you have no shell tool, reply exactly: NO-SHELL"
        ),
        working_dir=str(tmp_path),
        credential=_credential(),
        model=None,
        agent_id=dsh_env,
        timeout=FIRST_TURN_TIMEOUT,
    )
    assert "LEAKED" not in scoped.text, (
        "the leaf ran a shell command — the scoped patch is not taking effect: "
        + scoped.text[:400]
    )


@requires_cli
@pytest.mark.asyncio
async def test_a_dangling_resume_id_is_classified_as_a_stale_session(tmp_path, dsh_env):
    """A resume id DSH no longer knows must be recognised as stale: that is what
    makes the once-per-turn recovery drop the id and start a fresh conversation
    instead of failing every turn forever. DSH's store is cwd- and
    machine-bound, so this is a case that really happens — and the pattern table
    behind it is a guess unless something exercises it, which is what this
    does."""
    run = _run(dsh_env)
    text = ""
    try:
        await _start(
            run,
            "Reply with exactly: PONG",
            str(tmp_path),
            resume_id="00000000-0000-4000-8000-000000000000",
        )
        events = await _drain(run, TURN_TIMEOUT)
        text = "\n".join(e.content or "" for e in events)
    except Exception as exc:  # a failed handshake raises before any event
        text = f"{exc}\n{run.stderr_text}"
    finally:
        await run.stop()

    assert get_harness("dsh").is_stale_session_error(text), (
        "a dangling resume id was not recognised as a stale session. The real "
        "error text was:\n" + text[:1500] + "\n\nUpdate "
        "_DSH_STALE_SESSION_PATTERNS in server/harness/dsh.py to match it."
    )
