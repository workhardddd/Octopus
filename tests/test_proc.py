"""`server/proc.py` — the platform-neutral process-group vocabulary.

The reaping itself is proven with real processes in `test_harness_core.py`
(a spawned child's child must die with the group). What lives here is what must
be true *before* anything is signalled: which spawn kwargs isolate a group, and
that asking about a process never ends it.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys

import pytest

from server.proc import IS_WINDOWS, kill_group, pid_alive, spawn_kwargs, terminate_group


def test_spawn_kwargs_isolate_the_group_on_this_platform():
    kwargs = spawn_kwargs()
    if IS_WINDOWS:
        assert kwargs["creationflags"] & subprocess.CREATE_NEW_PROCESS_GROUP
        # `start_new_session` is a POSIX spelling Windows ignores — if it ever
        # came back here, a Windows child would silently share our group and
        # `taskkill /T` would reach us.
        assert "start_new_session" not in kwargs
    else:
        assert kwargs == {"start_new_session": True}


@pytest.mark.asyncio
async def test_pid_alive_does_not_kill_the_process_it_probes():
    """The trap this module exists for: `os.kill(pid, 0)` is a probe on POSIX
    and a *kill* on Windows (CPython spells it `TerminateProcess(handle, 0)`),
    so the obvious implementation destroys what it is asking about — and,
    against an already-reaped pid the OS has recycled, destroys a stranger."""
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-c", "import time; time.sleep(30)"
    )
    try:
        assert pid_alive(proc.pid) is True
        await asyncio.sleep(0.2)
        assert proc.returncode is None, "the probe killed the process it asked about"
        assert pid_alive(proc.pid) is True
    finally:
        kill_group(proc)
        await proc.wait()


def test_pid_alive_is_false_for_a_pid_that_cannot_exist():
    # Above Linux's pid ceiling (pid_max defaults to 4194304) but still inside
    # C `int`, and odd — Windows pids are always multiples of 4 — so no process
    # can hold it while the assertion runs.
    assert pid_alive(0x7FFFFFFF) is False


@pytest.mark.asyncio
async def test_group_helpers_are_best_effort_on_a_finished_process():
    """Every caller is mid-teardown and racing the exit, so a process that is
    already gone must be a False, not an exception."""
    proc = await asyncio.create_subprocess_exec(sys.executable, "-c", "pass")
    await proc.wait()
    assert terminate_group(proc) is False
    assert kill_group(proc) is False
