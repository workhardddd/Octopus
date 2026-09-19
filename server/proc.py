"""Process control that has to work on both platforms, in one place.

A child that must die as a *unit* — together with everything it spawned —
needs a different mechanism per platform, and getting it wrong fails silently:
the direct child exits and its grandchildren keep running with no parent
(`turn-safety.md` §2, `application-backends.md`, the bg-task shell).

  * POSIX: spawn it as a session leader (`start_new_session=True`) and signal
    the whole group with `os.killpg`.
  * Windows: spawn it with `CREATE_NEW_PROCESS_GROUP` (its pid is then also its
    group id), ask the group to stop with `CTRL_BREAK_EVENT`, and force the
    tree with `taskkill /F /T`, which walks parent→child links.

No caller names a signal: `signal.SIGKILL` does not exist on Windows at all,
which is why the two *intents* — "please stop" and "stop now" — are the API
here rather than signal numbers.

`pid_alive` lives here because `os.kill(pid, 0)` is **not** a liveness probe on
Windows: CPython implements `os.kill` there as `TerminateProcess(handle, sig)`,
so `os.kill(pid, 0)` *kills* the process and files it as exit code 0 — and if
that pid has already been reaped and recycled by the OS, it kills whatever
unrelated process holds it now. On POSIX the very same call is the correct,
harmless idiom, which is what makes it so easy to carry across.

The same reasoning bounds the *group* signals: they reach `pgid`, not `pid`, so
a pid that has been recycled can point at a group we are standing in. Both
`terminate_group` and `kill_group` therefore signal only a process that leads its
own group — which `spawn_kwargs()` guarantees for everything this app spawns,
and which a caller that bypassed it does not get.
"""

from __future__ import annotations

import asyncio
import ctypes
import os
import signal
import subprocess
from typing import Any

IS_WINDOWS = os.name == "nt"

# PROCESS_QUERY_LIMITED_INFORMATION: enough to ask about a process, not enough
# to end one — so a probe built on it can never kill by accident.
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_STILL_ACTIVE = 259

_win32_cache: tuple[Any, Any] | None = None


def spawn_kwargs() -> dict[str, Any]:
    """`create_subprocess_exec` kwargs that make the child a group leader.

    Both spellings are honoured by `asyncio.create_subprocess_exec` and by
    `subprocess.Popen`; each is a no-op on the other platform, so callers pass
    whichever this returns and never ask which platform they are on.
    """
    if IS_WINDOWS:
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def terminate_group(proc: asyncio.subprocess.Process) -> bool:
    """Ask the child's whole group to stop — SIGTERM on POSIX, a console
    `CTRL_BREAK_EVENT` on Windows.

    Best-effort: a process that is already gone is not an error. Returns True
    if the request reached the *group* (False when it fell back to the direct
    child, which is the caller's signal that children may have survived).
    """
    return _signal_group(proc, force=False)


def kill_group(proc: asyncio.subprocess.Process) -> bool:
    """Force the child's whole group to stop — SIGKILL on POSIX,
    `taskkill /F /T` on Windows. Same contract as `terminate_group`."""
    return _signal_group(proc, force=True)


def pid_alive(pid: int) -> bool:
    """Is `pid` still running?

    `os.kill(pid, 0)` on POSIX, where that is a real probe, and OpenProcess +
    GetExitCodeProcess on Windows, where it is not (module docstring).

    Only meaningful for a pid the OS cannot have recycled yet: capture it, kill,
    and check immediately — Windows reuses pids eagerly. It is used by the
    process-group tests, which is also why the Windows branch must be able to
    ask about a process it does not own.
    """
    if not IS_WINDOWS:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True  # it exists; simply not ours to signal
        return True

    kernel32, wintypes = _win32()
    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == _STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


# ----------------------------------------------------------------- internals


def _signal_group(proc: asyncio.subprocess.Process, *, force: bool) -> bool:
    if proc.returncode is not None:
        return False
    if IS_WINDOWS:
        return _windows_signal_group(proc, force=force)
    try:
        pgid = os.getpgid(proc.pid)
    except (ProcessLookupError, PermissionError, OSError):
        pgid = None
    # Only a *group leader* is ours to signal, and `spawn_kwargs()` is what makes
    # every child one (its pid is its group id). Without this check the pid alone
    # decides which group gets the signal, and for a process that was never
    # isolated — or one the OS has already reaped and recycled, which `pid_alive`
    # exists for one step earlier — the answer can be a group we are standing in.
    # `killpg` would then take down unrelated processes, ourselves included.
    # (Found for real: a test spawned a child without `spawn_kwargs()` and then
    # group-killed it, which killed the container's init and the whole test run.)
    if pgid != proc.pid:
        pgid = None
    if pgid is not None:
        try:
            os.killpg(pgid, signal.SIGKILL if force else signal.SIGTERM)
            return True
        except (ProcessLookupError, PermissionError, OSError):
            pass
    # No group of ours to reach (never made one, it is already gone, or the pid
    # is not the leader): the direct child is still worth signalling — and the
    # False return says children may have outlived it.
    try:
        if force:
            proc.kill()
        else:
            proc.terminate()
    except (ProcessLookupError, PermissionError, OSError):
        pass
    return False


def _windows_signal_group(proc: asyncio.subprocess.Process, *, force: bool) -> bool:
    """Windows has no signals, so the graceful intent is spelled
    `CTRL_BREAK_EVENT`: it reaches the whole group, but only one whose leader
    asked to be a group (`spawn_kwargs()`), and only while a console exists to
    carry it. When either is missing we go straight to the forceful step."""
    if not force:
        try:
            os.kill(proc.pid, signal.CTRL_BREAK_EVENT)
            return True
        except (OSError, ValueError):
            pass  # no console for the group — taskkill it is
    if _taskkill_tree(proc.pid):
        return True
    try:
        proc.kill()
    except (ProcessLookupError, PermissionError, OSError):
        pass
    return False


def _taskkill_tree(pid: int) -> bool:
    """`taskkill /F /T` ends a process *and its descendants* — the closest
    Windows equivalent of killing a process group.

    Blocking on purpose: every caller is mid-teardown and holds no lock, and
    the alternative is an awaitable threaded through four modules for a call
    that normally returns in milliseconds.
    """
    try:
        done = subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return done.returncode == 0


def _win32() -> tuple[Any, Any]:
    """`(kernel32, ctypes.wintypes)` with the prototypes this module uses.

    Imported lazily and cached: `ctypes.wintypes` does not exist off Windows,
    so it cannot be a module-level import in a file every platform loads.
    """
    global _win32_cache
    if _win32_cache is None:
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.GetExitCodeProcess.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.DWORD),
        ]
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        _win32_cache = (kernel32, wintypes)
    return _win32_cache
