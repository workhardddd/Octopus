#!/usr/bin/env python3
"""Run the backend suite — the fast way, on either platform.

Two things made this loop slow enough to matter (measured from a real session's
DSH log: 200 minutes of tool time, 125 of it blocked in front of background
jobs):

* **The Linux container re-installed its environment on every run** — an
  `apt-get install git` plus a `pip install`, four minutes of setup around forty
  seconds of tests, repeated dozens of times. The environment is now an image
  (`test-linux.Dockerfile`) that is built once and reused.
* **Nothing said which interpreter actually has the dependencies.** On a box
  whose `.venv` is a stub, `.venv/bin/pytest` does not exist and a bare
  `python -m pytest` cannot import `pydantic_settings`, so the only thing that
  worked was the container — with its four-minute setup. This script picks an
  interpreter that can import the suite's dependencies, and says which one it
  picked and why.

    python scripts/test.py                      # whole suite, native
    python scripts/test.py tests/test_proc.py   # one file — seconds
    python scripts/test.py --fast                # the suite minus the real-CLI modules
    python scripts/test.py -k dsh --linux       # …in the Linux container
    python scripts/test.py --show-python        # just report the interpreter

Iterate with a path (a file or two runs in seconds). `--fast` is the bucket for
a dev box whose CLIs are busy or logged out: it deselects the `*_real.py`
modules *before* they import, so their availability probes never run — a probe
that cannot answer blocks the whole collection for minutes and then aborts it,
which is how a fast edit-and-check loop becomes a twenty-minute wait. It says
out loud which modules it dropped; the full suite still has to pass before a
commit.

Diagnostics are read with `job_output`, never by piping this into
`Select-Object -Last`/`tail`-on-a-live-process — a long-lived command's pipe
never reaches EOF, so the job looks like it is still running forever.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
IMAGE = os.environ.get("OCTOPUS_TEST_IMAGE", "octopus-test:3.12")
DOCKERFILE = REPO_ROOT / "scripts" / "test-linux.Dockerfile"

#: What the suite imports at collection time. If an interpreter cannot import
#: these, it cannot run the suite at all — checking is a two-second probe
#: against a four-minute container build.
REQUIRED = ("pytest", "pytest_asyncio", "pydantic_settings", "fastapi", "mcp")


def _candidates() -> list[tuple[str, Path | str]]:
    """Interpreters to try, most specific first."""
    out: list[tuple[str, Path | str]] = []
    named = os.environ.get("OCTOPUS_TEST_PYTHON")
    if named:
        out.append(("$OCTOPUS_TEST_PYTHON", named))
    venv = (
        REPO_ROOT / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    )
    if venv.exists():
        out.append(("checkout venv", venv))
    out.append(("this interpreter", sys.executable))
    return out


def _can_run(python: Path | str) -> bool:
    probe = "import " + ", ".join(REQUIRED)
    try:
        done = subprocess.run(
            [str(python), "-c", probe],
            capture_output=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0


def pick_python(*, verbose: bool) -> str:
    tried = []
    for label, python in _candidates():
        if _can_run(python):
            if verbose:
                print(f"[test] interpreter: {python} ({label})", flush=True)
            return str(python)
        tried.append(f"{python} ({label})")
    print(
        "[test] no interpreter can import the suite's dependencies.\n"
        "       tried:\n"
        + "".join(f"         - {t}\n" for t in tried)
        + "       install them into the checkout venv with:\n"
        '         .venv/bin/python -m pip install -e ".[test]"\n'
        "       (add `-i <index-url>` if this network needs a mirror; see\n"
        "       docs/plans/windows-support.md §6)",
        file=sys.stderr,
    )
    raise SystemExit(2)


def _run(argv: list[str], *, cwd: Path | None = None) -> int:
    started = time.monotonic()
    done = subprocess.run(argv, cwd=str(cwd) if cwd else None)
    print(f"[test] {time.monotonic() - started:.1f}s  ({' '.join(argv[:3])} …)", flush=True)
    return done.returncode


def real_cli_modules() -> list[str]:
    """The `*_real.py` files, which is exactly the set that gates on a CLI.

    Naming is the contract: those modules import a live `claude` / `codex` /
    `dsh` and probe it at import time. Deselecting them is how `--fast` avoids
    the probe rather than paying for it.
    """
    return sorted(
        str(p.relative_to(REPO_ROOT))
        for p in (REPO_ROOT / "tests").glob("*_real.py")
    )


def _docker() -> str:
    exe = shutil.which("docker")
    if not exe:
        print(
            "[test] --linux needs Docker on PATH. Run the native suite instead\n"
            "       (drop --linux); it covers everything except the handful of\n"
            "       POSIX-only cases.",
            file=sys.stderr,
        )
        raise SystemExit(2)
    return exe


def _image_present(docker: str) -> bool:
    done = subprocess.run(
        [docker, "image", "inspect", IMAGE], capture_output=True
    )
    return done.returncode == 0


def build_image(docker: str, *, verbose: bool = True) -> None:
    """Build the test image once. Reused by every later `--linux` run."""
    argv = [docker, "build", "-f", str(DOCKERFILE), "-t", IMAGE]
    # A mirror is this machine's business, not the repo's: pass one through only
    # when the environment asks for it. `OCTOPUS_TEST_BASE_IMAGE` is for a box
    # whose Docker registry mirror cannot reach Docker Hub at all (the failure
    # is a DNS error on `FROM`, before anything else in this file runs).
    for arg, env in (
        ("PIP_INDEX_URL", "PIP_INDEX_URL"),
        ("APT_MIRROR", "APT_MIRROR"),
        ("BASE_IMAGE", "OCTOPUS_TEST_BASE_IMAGE"),
    ):
        value = os.environ.get(env)
        if value:
            argv += ["--build-arg", f"{arg}={value}"]
    argv.append(str(REPO_ROOT))
    if verbose:
        print(f"[test] building {IMAGE} (one time)…", flush=True)
    done = subprocess.run(argv)
    if done.returncode != 0:
        raise SystemExit(done.returncode)


def run_linux(extra: list[str], *, verbose: bool) -> int:
    docker = _docker()
    if not _image_present(docker):
        build_image(docker, verbose=verbose)
    argv = [
        docker,
        "run",
        "--rm",
        # An init as PID 1, because the suite has a test that kills a process
        # *group* and then asserts the grandchild is gone. Without an init, a
        # container's PID 1 is pytest itself, which never reaps a child it did
        # not spawn: the killed grandchild is re-parented to it and stays a
        # zombie, `pid_alive` still sees the pid, and the test fails in the
        # container while passing on a real Linux box — i.e. it would be
        # measuring the container's init, not our group kill.
        "--init",
        "-v",
        f"{REPO_ROOT}:/app",
        "-w",
        "/app",
        # The mounted checkout IS the code under test; no install of the
        # project itself, so the image never carries a stale snapshot.
        "-e",
        "PYTHONPATH=/app",
    ]
    if os.environ.get("DEEPSEEK_API_KEY"):
        # The one real-CLI gate the container can satisfy: DSH authenticates
        # with a key rather than a login a CI box could hold.
        argv += ["-e", "DEEPSEEK_API_KEY"]
    argv += [IMAGE, "python", "-m", "pytest", *extra]
    return _run(argv)


def main() -> int:
    argv = sys.argv[1:]
    if "-h" in argv or "--help" in argv:
        print(__doc__)
        return 0

    # Split by hand rather than with argparse: everything that is not one of our
    # flags belongs to pytest, in the order the caller wrote it (`-k dsh` must
    # not arrive as `dsh -k`).
    ours = {"--linux", "--fast", "--build-only", "--show-python"}
    flags = {a for a in argv if a in ours}
    extra = [a for a in argv if a not in ours]

    if "--build-only" in flags:
        build_image(_docker())
        return 0

    if "--show-python" in flags:
        print(pick_python(verbose=False))
        return 0

    if "--fast" in flags:
        dropped = real_cli_modules()
        extra += [f"--ignore={path}" for path in dropped]
        print(
            f"[test] --fast: {len(dropped)} real-CLI module(s) deselected before "
            "import, so their CLI probes never block collection:\n"
            + "".join(f"         - {path}\n" for path in dropped)
            + "       The rest of the suite runs; a full (non---fast) pass is "
            "still required before committing.",
            flush=True,
        )

    if not extra or extra == ["tests/", "-q"]:
        extra = ["tests/", "-q"]
    extra = [*extra, "--durations=10"]

    if "--linux" in flags:
        return run_linux(extra, verbose=True)

    python = pick_python(verbose=True)
    return _run([python, "-m", "pytest", *extra], cwd=REPO_ROOT)


if __name__ == "__main__":
    sys.exit(main())
