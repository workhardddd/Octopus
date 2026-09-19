# Tech Plan: Running Octopus on Windows

Status: **implemented** (2026-09-19) — the server boots natively on Windows and
every process it spawns there is killable as a unit. What it fixed, how, what it
cost POSIX, and what is still POSIX-only:

## 0. Why this exists

The owner's daily machine is Windows, and until this change **nothing ran on
it**:

- `python -c "import server.main"` died at import. `server/app_backends.py`
  evaluated `signal.SIGKILL` as a *default argument*, and Windows has no such
  constant — so the module (and every module importing it) could not be
  imported at all, on any code path. Sixteen test files failed at collection.
- Four places killed a child by hand with `os.getpgid` / `os.killpg` (the
  harness turn engine, `bg_tasks`, `codex_login`, `app_backends`). Both calls
  are POSIX-only, and their failure mode is *silent*: the direct child exits and
  its grandchildren keep running with no parent.
- `bg_tasks` ran every command through a hard-coded `/bin/sh`.

And one hazard learned by tripping it, which is why the module below also owns
liveness:

- `os.kill(pid, 0)` is a harmless liveness probe on POSIX and a **kill** on
  Windows — CPython spells `os.kill` there as `TerminateProcess(handle, sig)`,
  so `sig=0` ends the process and files it as exit code 0. Against a pid that has
  already been reaped and *recycled*, it ends whatever unrelated process holds
  that pid now. It is used in this repo's own tests, in the one place the
  POSIX idiom is correct and the Windows reading is destructive.

## 1. Design — `server/proc.py`

One small module owns the platform vocabulary; nothing else names a signal or
touches a process group.

| Call | POSIX | Windows |
|---|---|---|
| `spawn_kwargs()` | `start_new_session=True` | `creationflags=CREATE_NEW_PROCESS_GROUP` |
| `terminate_group(proc)` | `killpg(SIGTERM)` | `CTRL_BREAK_EVENT` to the group |
| `kill_group(proc)` | `killpg(SIGKILL)` | `taskkill /F /T /PID` |
| `pid_alive(pid)` | `os.kill(pid, 0)` | `OpenProcess(QUERY_LIMITED)` + `GetExitCodeProcess` |

- **Intent, not signals.** `signal.SIGKILL` does not exist on Windows, which is
  why the API is the two intents — *please stop* and *stop now* — rather than
  signal numbers. Each returns True only when the request reached the **group**;
  False means it fell back to the direct child, i.e. children may have survived.
- **Group first, child second, never an error.** Every caller is mid-teardown
  racing the exit, so a process that is already gone is a `False`, not a raise.
- **`CTRL_BREAK_EVENT` is a request, not a guarantee:** it needs a console and a
  group leader, and a child may ignore it. Every caller already escalates
  (`stdin` close → polite → forced with a bounded wait), so the polite step
  failing on Windows costs a grace period, never a leak.
- **`pid_alive` may never kill.** The Windows branch opens with
  `PROCESS_QUERY_LIMITED_INFORMATION` — enough to ask, not enough to end — and
  the module docstring records the trap for the next reader.

## 2. The bg shell

A bg command's contract is POSIX shell syntax: the model is told so in the tool
description, and it is what lets one prompt work on every platform. So the shell
is *resolved*, not assumed:

- POSIX → `/bin/sh`.
- Windows → `sh` from PATH, else Git for Windows (`Git/bin/sh.exe`,
  `Git/usr/bin/sh.exe` under `%ProgramFiles%`, `%ProgramFiles(x86)%`,
  `%LOCALAPPDATA%`).
- The shell's own bin dirs (`bin`, `usr/bin`, `mingw64/bin`) go **ahead of
  PATH**, because Git keeps `sh` and its coreutils (`sleep`, `grep`, `sed`) off
  the machine PATH — without this every command using one exits 127.
- No shell found → the task is refused with an explanation. It is never handed
  to `cmd.exe`, where most of what the model writes would fail in ways it cannot
  see.

## 3. What this changed for POSIX

Intended: nothing. The one deliberate change is in `bg_tasks`: a bg task the
**idle watchdog** stops is now labelled `interrupted` from an explicit flag
(`force_stopped`) instead of being inferred from a negative exit code. On POSIX
the outcome is identical (a SIGKILLed process already read negative); on Windows
it is the only way to be right, because a console break exits `0xC000013A` and
`taskkill` exits 1 — both positive.

## 4. Files

**New**

| File | Purpose |
|---|---|
| `server/proc.py` | The platform vocabulary above: group isolation at spawn, group terminate/kill, non-destructive liveness. |
| `tests/test_proc.py` | The per-platform spawn kwargs, and that probing a process never ends it. |

**Changed**

| File | Change |
|---|---|
| `server/harness/run.py` | `prepare_spawn` → `spawn_kwargs()`; `stop()`'s escalation → `terminate_group`/`kill_group`; the private `_terminate_process_group` is gone. |
| `server/harness/harness.py` | `run_oneshot`'s reap → `kill_group`. |
| `server/app_backends.py` | The import-time `signal.SIGKILL` default that blocked every import; spawns and the stop escalation go through the module. |
| `server/bg_tasks.py` | Shell resolution + PATH augmentation; group stop/force through the module; `force_stopped`. |
| `server/codex_login.py` | Group kill through the module; spawn kwargs. |
| `server/applications.py`, `server/routers/` | `os.path.commonpath` no longer raises when a probe path shares no drive with the managed root — a traversal probe returned 500 instead of 404 on Windows. |
| `server/mcp_servers/bg.py` | The tool description says "a POSIX shell (`sh -c`)", which is true on both platforms. |
| `web/playwright.config.ts` | The e2e webServer runs `$OCTOPUS_E2E_PYTHON` (defaulting to the checkout's venv, `Scripts` on Windows) — this box has no PyPI access, so its interpreter is named by the environment instead. |

## 5. Tests

- `tests/test_proc.py` — 4 cases: the per-platform spawn kwargs; **probing a
  live process does not kill it** (the incident, as a regression test); an
  impossible pid is False; the helpers are best-effort on a finished process.
- `tests/test_harness_core.py` — the group-reaping test now spawns Python
  children instead of `sh -c "sleep 30 &"`, so it runs on both platforms, and
  the spawn-kwargs assertion is per-platform.
- `tests/test_bg_tasks.py` — shell resolution, the PATH augmentation, and the
  watchdog interruption label.
- Two tests carry a `skipif(os.name == "nt")` with the reason: the fallback-PATH
  test (a systemd premise, and a `#!/bin/sh` probe) and the external-`SIGTERM`
  test (asserts the negative exit code a POSIX signal leaves).
- Five unrelated tests were making a POSIX premise they did not have to make and
  now run everywhere: four asserted a stored `working_dir` as `/tmp` (the route
  normalizes to an absolute path, which on Windows is `D:\tmp`), and
  `tests/test_cli_gate.py` used the POSIX `true`/`false` binaries — which do not
  exist on Windows, so "a logged-out CLI exits non-zero" passed for the wrong
  reason. `tests/test_file_viewer.py`'s fixture wrote its sample file in text
  mode (CRLF inflated the byte count), and `test_session_duplicate`'s HOME
  isolation is still open (§7).

Counts: **1169** backend tests (was 1160). Authoritative run in the Linux
container: **1127 passed / 42 skipped / 0 failed**.

## 6. Verified where

- **Windows** (this machine, system Python, no shim): `import server.main`
  succeeds; the server boots and serves the built SPA on `:8000`;
  `/api/backends` → `["dsh","claude-code","codex"]`; the real group-reaping test
  passes (a grandchild dies with its group); **the whole non-`*_real` suite runs
  natively — 1117 passed / 14 skipped / 0 failed**; **Playwright's `:fast` bucket
  41/41** in a real browser against a real backend started by the config above —
  which includes the mocked DSH credential dialog.
- **Linux container**: the full suite — 1127 passed / 42 skipped / 0 failed —
  nothing skipped beyond the CLI gates and the two documented POSIX-only ones.

## 7. What this defers

Genuine deferrals — work that needs a decision or a design. **The Python suite is
green on both platforms now**: everything except the `*_real.py` suites runs
natively on Windows at 1117 passed / 14 skipped / **0 failed**, and in the Linux
container at 1127 passed / 42 skipped / 0 failed of 1169.

The 11 Windows skips are each a *platform premise the test cannot express*, not a
bug, and each states it in its own `reason`:

- **A symlink privilege this host does not have** (4: three icon/traversal guards
  in `test_applications.py`, one in `test_file_viewer.py`). `os.symlink` fails
  with `WinError 1314` unless the process is elevated or Developer Mode is on, so
  the guard cannot be exercised here at all — a capability skip through
  `tests/capabilities.py::can_symlink()`, the same shape as the CLI gates.
- **Applications' backend scripts** (4 in `test_applications.py`).
  `start.sh`/`install.sh` are executed directly, which Windows cannot do for a
  `.sh` — it would need the POSIX shell from §2 plus a decision about shebangs —
  and `os.access(path, os.X_OK)` is an existence check there, so "declared by an
  *executable* script" has no Windows meaning. **This one is a real product gap,
  tracked as its own decision**; everything else in Applications works.
- **A POSIX premise in the test itself** (3): `test_subprocess_path` asserts the
  POSIX fallback dirs (`~/.local/bin`, `/usr/local/bin`, `/opt/homebrew/bin`);
  the two `test_fork_native_copy` cases use Claude Code's POSIX project layout
  (`~/.claude/projects/-x-y`) and pin the POSIX project-slug encoding, which is
  not the same string on Windows.

Two more were *bugs in the tests*, fixed rather than skipped: tests that isolated
`$HOME` resolved to the developer's real profile on Windows (which reads
`USERPROFILE`) and wrote into it — `~/.octopus/fork/…`, `~/.claude/projects/…`.
`tests/capabilities.py::isolate_home` sets both, and every such test uses it now.

Also deferred:

- **A `sh` that is not Git for Windows** (Cygwin, MSYS2, WSL) — the resolver
  knows the two standard Git layouts; anything else must be on PATH.
- **`token_rotation`'s `os.chmod(0o600)`** is a no-op on Windows, where file
  permissions are ACLs: the rewritten env file inherits its directory's
  permissions. Not a regression, but it is not the guarantee the POSIX doc
  describes.

