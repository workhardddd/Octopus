"""Shared real-CLI availability gates for the `tests/*_real.py` suites.

Real-LLM tests must skip not just when a backend's binary is absent, but also
when it's present yet NOT signed in — otherwise an expired/lapsed login turns
the whole suite red with confusing downstream errors ("one-shot exited 1",
AI-parse failures) that look like product regressions but are really just a
logged-out CLI. (That's the exact failure harness-credential-reauth.md exists
to surface in the app.)

These probe ACTUAL usability once per session (lru_cache):
  - claude: a tiny real `--print` call must exit 0 (there's no auth.json to stat).
  - codex:  binary present AND ~/.codex/auth.json exists (a real call is slower;
            the login file is the same signal codex itself uses).

Imported as `from tests.cli_gate import claude_cli_works, codex_cli_works`
(the repo root is on sys.path during the test run, so `tests` resolves as a
namespace package — `import conftest` is NOT reliable under pytest).
"""

from __future__ import annotations

import functools
import os
import shutil
import subprocess

from server.proc import kill_group, spawn_kwargs


def _resolve_cli(binary: str) -> str | None:
    """Resolve a CLI honoring the same PATH fallback the harness uses (nvm /
    ~/.local/bin), so the gate matches how the backend actually launches it."""
    try:
        from server.harness.run import _which_with_fallback

        return _which_with_fallback(binary)
    except Exception:
        return shutil.which(binary)


class CliProbeTimeout(RuntimeError):
    """A gate probe timed out, twice.

    Deliberately an error rather than a `False`. `False` means "this CLI is
    absent or logged out", which turns dependent tests into skips — and a skip
    that really meant "the box was busy" is a silently hollow suite, the exact
    thing these gates exist to prevent. A timeout is not evidence about the
    login; it's evidence we couldn't tell, so the suite says so out loud.
    """


def _probe(argv: list[str], *, timeout: float, cwd: str | None = None) -> bool:
    """Run a gate probe, retrying once with a doubled timeout.

    A loaded machine (a parallel suite, several CLIs mid-turn) can push a
    trivial call past its limit; one retry absorbs that. Two timeouts in a row
    is not load, and is reported rather than swallowed.

    Two things here exist because of a measured session, where the gates turned
    a twenty-minute test run into a mystery:

    * **It says what it is doing.** A silent probe that takes four minutes looks
      exactly like a hung test run, which is how a person ends up re-running
      things and waiting twice.
    * **A timeout kills the process GROUP.** `subprocess.run(timeout=…)` kills
      only the direct child and then keeps reading its pipes until EOF, so a CLI
      that leaves a grandchild behind overruns the deadline (measured on this
      repo's own box: a 20s limit returned after 25.6s). The group kill is what
      makes the stated limit true, which is what makes "90s then 180s" a bound
      someone can reason about.
    """
    last: str = "no attempt was made"
    for limit in (timeout, timeout * 2):
        try:
            proc = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=cwd,
                **spawn_kwargs(),
            )
        except OSError as exc:
            return False
        try:
            proc.communicate(timeout=limit)
        except subprocess.TimeoutExpired:
            _kill_probe(proc)
            last = f"{' '.join(argv)} timed out after {limit:.0f}s"
            print(f"[cli-gate] {last}; retrying once", flush=True)
            continue
        return proc.returncode == 0
    raise CliProbeTimeout(
        f"{argv[0]} did not answer a trivial probe within "
        f"{timeout:.0f}s or {timeout * 2:.0f}s. This is NOT a lapsed login — "
        f"tests must not be skipped on it. Re-run on a less loaded machine, "
        f"or fix the CLI. (last: {last})"
    )


def _kill_probe(proc: subprocess.Popen) -> None:
    """Stop a probe that overran, group first, then make sure it is reaped.

    Every step is best-effort: a probe is advisory until it answers, and a
    failure to clean up must not replace the gate's own diagnosis with an
    unrelated OSError.
    """
    try:
        kill_group(proc)
    except Exception:
        pass
    try:
        proc.communicate(timeout=10)
    except (subprocess.TimeoutExpired, OSError):
        try:
            proc.kill()
        except Exception:
            pass


@functools.lru_cache(maxsize=1)
def claude_cli_works() -> bool:
    """True only if `claude` is installed AND authenticated. Probes once with a
    minimal real call; a logged-out CLI exits non-zero (the 401), so dependent
    tests skip rather than fail."""
    exe = _resolve_cli("claude")
    if exe is None:
        return False
    return _probe([exe, "--print", "--", "ok"], timeout=60)


@functools.lru_cache(maxsize=1)
def codex_cli_works() -> bool:
    """True only if `codex` is installed AND actually authenticated. A present
    `~/.codex/auth.json` is NOT sufficient — its token can be invalidated while
    the file lingers (a real 401 we hit in practice). So we probe with a tiny
    real `codex exec`, exactly like the claude gate; a logged-out CLI exits
    non-zero, so dependent tests skip rather than hollow-pass/fail."""
    exe = _resolve_cli("codex")
    if exe is None:
        return False
    import tempfile

    # A 401 / invalidated token exits non-zero and prints the auth error.
    return _probe(
        [
            exe, "exec", "--json", "--skip-git-repo-check",
            "--dangerously-bypass-approvals-and-sandbox", "--", "Reply with OK.",
        ],
        timeout=90,
        cwd=tempfile.gettempdir(),
    )


@functools.lru_cache(maxsize=1)
def dsh_cli_present() -> bool:
    """True if the `dsh` binary resolves (honoring the harness's PATH
    fallback). The shallow half of the DSH gate: enough for tests that only
    need the CLI to exist (a rejected credential, for instance)."""
    return _resolve_cli("dsh") is not None


@functools.lru_cache(maxsize=1)
def dsh_cli_works() -> bool:
    """True if `dsh` is installed AND has a usable credential.

    DSH authenticates with an API key rather than a login flow, so the key's
    presence in the environment is what makes a turn possible. The probe is
    deliberately shallow — `--version`, not a real turn — because a real turn
    would need a `DSH_HOME`, and initializing one just to answer "is this CLI
    usable" is slow, needs the network, and (against the default home) would
    write a session into whatever the person running the tests uses DSH for.
    Tests that need a turn check the key and then prove it by running one.
    """
    exe = _resolve_cli("dsh")
    if exe is None or not os.environ.get("DEEPSEEK_API_KEY"):
        return False
    return _probe([exe, "--version"], timeout=60)
