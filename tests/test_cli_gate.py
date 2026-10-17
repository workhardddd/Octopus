"""The real-CLI gates decide whether the `*_real.py` suites run at all, so a
gate that answers wrongly is worse than a failing test: it turns 18 real tests
into skips and the suite still reads green.

CLAUDE.md's rule is that a skip means a lapsed login, not a passing suite.
These tests pin the distinction the gate has to draw — absent/logged-out is a
skip, "we couldn't tell" is not — and the two properties that keep a gate from
eating a session: the stated limit is the real limit (the whole process group is
stopped, not just the direct child), and a slow probe says so.
"""

from __future__ import annotations

import dataclasses
import subprocess
import sys

import pytest

from tests import cli_gate
from tests.cli_gate import CliProbeTimeout, _probe

# The gate only reads the exit status, so the stand-ins are this interpreter
# rather than the POSIX `true`/`false` binaries — which do not exist on Windows,
# where they made the "a logged-out CLI exits non-zero" case pass for the wrong
# reason.
_ANSWERS = [sys.executable, "-c", "pass"]
_FAILS = [sys.executable, "-c", "raise SystemExit(1)"]


@dataclasses.dataclass
class _Record:
    """What the gate did to the fake CLI."""

    answers_on: int
    timeouts: list[float] = dataclasses.field(default_factory=list)
    started: list["_FakeProc"] = dataclasses.field(default_factory=list)
    killed: list["_FakeProc"] = dataclasses.field(default_factory=list)
    killed_directly: list["_FakeProc"] = dataclasses.field(default_factory=list)


class _FakeProc:
    """A CLI that overruns `communicate` until the record says otherwise."""

    def __init__(self, argv, record: _Record, **kwargs):
        self.argv = argv
        self.kwargs = kwargs
        self.returncode = None
        self.pid = 4242
        self._record = record
        record.started.append(self)

    def communicate(self, timeout=None):
        self._record.timeouts.append(timeout)
        if len(self._record.timeouts) < self._record.answers_on:
            raise subprocess.TimeoutExpired(self.argv, timeout)
        self.returncode = 0
        return b"", b""

    def kill(self):
        self._record.killed_directly.append(self)
        self.returncode = -9


@pytest.fixture
def fake_cli(monkeypatch):
    """Install the fake process seam; returns the run record."""

    def install(*, answers_on: int) -> _Record:
        record = _Record(answers_on=answers_on)
        monkeypatch.setattr(
            cli_gate.subprocess,
            "Popen",
            lambda argv, **kwargs: _FakeProc(argv, record, **kwargs),
        )
        monkeypatch.setattr(cli_gate, "kill_group", record.killed.append)
        return record

    return install


def test_probe_returns_true_when_the_cli_answers():
    assert _probe(_ANSWERS, timeout=10) is True


def test_probe_returns_false_when_the_cli_fails():
    """A logged-out CLI exits non-zero — that IS evidence, so dependent tests
    legitimately skip."""
    assert _probe(_FAILS, timeout=10) is False


def test_probe_returns_false_when_the_binary_is_missing():
    assert _probe(["/nonexistent-binary-for-gate-test"], timeout=10) is False


def test_probe_retries_once_before_giving_up(fake_cli):
    """A loaded box can push a trivial call past its limit; one retry absorbs
    that rather than declaring the CLI logged out."""
    record = fake_cli(answers_on=2)

    assert _probe(_ANSWERS, timeout=5) is True

    # Second attempt gets a longer budget than the first… (the trailing entry is
    # the bounded reap of the killed attempt, not a third probe)
    assert record.timeouts[:2] == [5, 10]
    assert record.killed_directly == [], "the group kill should have been enough"
    # …and the attempt that overran was stopped as a *group*, so the next one
    # starts when the limit says it does. `subprocess.run(timeout=…)` kills only
    # the direct child and then keeps reading its pipes until EOF: measured on
    # this repo's own box, a 20s limit returned after 25.6s.
    assert record.killed == [record.started[0]]


def test_two_timeouts_raise_instead_of_skipping(fake_cli):
    """The important one. Two timeouts mean we could not tell whether the CLI
    works — which must never be reported as "logged out", because that silently
    hollows out the suite."""
    record = fake_cli(answers_on=99)

    with pytest.raises(CliProbeTimeout, match="NOT a lapsed login"):
        _probe(["whatever"], timeout=1)

    assert len(record.started) == 2
    assert record.killed == record.started, "both overruns must be stopped"


def test_a_slow_probe_says_so(capsys, fake_cli):
    """Silence is what makes a slow gate look like a hung run, which is what
    turned a twenty-minute collection into a mystery. Someone reading the log
    has to be able to tell "working" from "waiting on a CLI that will never
    answer"."""
    fake_cli(answers_on=2)

    _probe(_ANSWERS, timeout=5)

    out = capsys.readouterr().out
    assert "[cli-gate]" in out
    assert "retrying once" in out
