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

import server.proc as proc_mod
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
        sys.executable,
        "-c",
        "import time; time.sleep(30)",
        # The product's own spawn shape: this child leads its own group, so the
        # teardown below signals *it* rather than the group the test itself is
        # standing in.
        **spawn_kwargs(),
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


@pytest.mark.skipif(IS_WINDOWS, reason="pgid semantics are POSIX-only")
def test_a_group_is_only_signalled_when_the_process_leads_it(monkeypatch):
    """A process we did not isolate must never cost us *our* group.

    The group signal goes to `pgid`, and the pid is all we hold — so for a child
    spawned without `spawn_kwargs()` (or for a recycled pid) that pgid is
    whichever group the process landed in, which can be ours. `killpg` there
    would take down unrelated processes, ourselves first: found for real, when a
    test spawned a child this way and the group kill took the container's init
    and the whole test run with it. The guard is that everything this app spawns
    leads its own group, so `pgid == pid` — anything else is refused and the
    direct child is stopped instead.
    """
    signalled: list[tuple[int, int]] = []
    monkeypatch.setattr(proc_mod.os, "getpgid", lambda pid: 4242)
    monkeypatch.setattr(proc_mod.os, "killpg", lambda *args: signalled.append(args))

    class _Foreign:
        pid = 999
        returncode = None
        killed = False

        def kill(self):
            self.killed = True

        def terminate(self):  # pragma: no cover — the forceful path is the one
            pass

    proc = _Foreign()
    assert kill_group(proc) is False
    assert signalled == [], "killpg was handed a group this process does not lead"
    assert proc.killed is True, "the direct child is still stopped"


# --- output decoding (CLI output is not always UTF-8) -----------------------


def test_decode_output_reads_utf8_first():
    assert proc_mod.decode_output("héllo\n".encode("utf-8")) == "héllo\n"


def test_decode_output_falls_back_to_the_ansi_code_page():
    """GBK bytes for 命令行太长 — cmd.exe's "command line too long", the real
    stderr a Chinese-locale Windows produces — must arrive as words, not the
    U+FFFD slush errors="replace" alone produces."""
    raw = "命令行太长。".encode("gbk")
    assert proc_mod.decode_output(raw).rstrip("\r\n") == "命令行太长。"
    # not just Chinese: any non-UTF-8 bytes decode as *something* readable-ish
    # rather than a wall of replacement characters.
    assert "���" not in proc_mod.decode_output(b"\x81\x40\x81\x41")


# --- npm shim resolution (Windows cmd.exe 8191-char command line) -----------


@pytest.mark.skipif(not IS_WINDOWS, reason="npm .cmd shims are a Windows launch path")
class TestResolveWindowsShim:
    def test_a_native_exe_shim_resolves_to_the_exe(self, tmp_path):
        exe = tmp_path / "pkg" / "bin" / "tool.exe"
        exe.parent.mkdir(parents=True)
        exe.write_bytes(b"MZ")
        shim = tmp_path / "tool.cmd"
        shim.write_text(
            "@ECHO off\r\nGOTO start\r\n:find_dp0\r\nSET dp0=%~dp0\r\nEXIT /b\r\n"
            ":start\r\nSETLOCAL\r\nCALL :find_dp0\r\n"
            f'"{exe}"   %*\r\n',
            encoding="utf-8",
        )
        assert proc_mod.resolve_windows_shim(str(shim)) == [str(exe)]

    def test_a_node_script_shim_resolves_to_node_plus_script(self, tmp_path, monkeypatch):
        script = tmp_path / "node_modules" / "pkg" / "cli.js"
        script.parent.mkdir(parents=True)
        script.write_text("// entry", encoding="utf-8")
        node = tmp_path / "node.exe"
        node.write_bytes(b"MZ")
        shim = tmp_path / "tool.cmd"
        shim.write_text(
            "@ECHO off\r\n:start\r\n"
            'IF EXIST "%dp0%\\node.exe" (\r\n  SET "_prog=%dp0%\\node.exe"\r\n'
            ') ELSE (\r\n  SET "_prog=node"\r\n)\r\n'
            f'endLocal & "%_prog%"  "{script}" %*\r\n',
            encoding="utf-8",
        )
        assert proc_mod.resolve_windows_shim(str(shim)) == [str(node), str(script)]

    def test_a_node_script_without_a_local_node_uses_which(self, tmp_path, monkeypatch):
        script = tmp_path / "cli.js"
        script.write_text("// entry", encoding="utf-8")
        shim = tmp_path / "tool.cmd"
        shim.write_text(f'endLocal & "%_prog%"  "{script}" %*\r\n', encoding="utf-8")
        found = tmp_path / "elsewhere" / "node.exe"
        monkeypatch.setattr(
            proc_mod.shutil, "which", lambda name: str(found) if name == "node" else None
        )
        assert proc_mod.resolve_windows_shim(str(shim)) == [str(found), str(script)]

    def test_an_unresolvable_shim_is_left_alone(self, tmp_path):
        shim = tmp_path / "tool.cmd"
        shim.write_text("@ECHO off\r\nweird-but-not-npm %*\r\n", encoding="utf-8")
        assert proc_mod.resolve_windows_shim(str(shim)) == [str(shim)]

    def test_a_missing_target_falls_back_to_the_shim(self, tmp_path):
        shim = tmp_path / "tool.cmd"
        shim.write_text(
            f'"{tmp_path / "gone.exe"}"   %*\r\n', encoding="utf-8"
        )
        assert proc_mod.resolve_windows_shim(str(shim)) == [str(shim)]

    def test_non_shims_pass_through_untouched(self, tmp_path):
        exe = tmp_path / "tool.exe"
        exe.write_bytes(b"MZ")
        assert proc_mod.resolve_windows_shim(str(exe)) == [str(exe)]
