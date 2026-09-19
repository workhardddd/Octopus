"""Host capabilities and platform quirks a few tests need and cannot assume.

Not the same thing as `cli_gate.py`: those gates are about a *CLI* being
installed and signed in (a lapse is a real problem). These are about the host
itself — a Windows process cannot create a symlink without Developer Mode or
elevation (`WinError 1314`), so a symlink-escape guard simply cannot be
exercised there. Saying so is better than deleting the test: the guard still
runs on the platforms that can create one.
"""

from __future__ import annotations

import functools
import os
import tempfile
from typing import Any


@functools.lru_cache(maxsize=1)
def can_symlink() -> bool:
    """Can this process create a symlink? Probed once, in a throwaway dir."""
    with tempfile.TemporaryDirectory() as root:
        target = os.path.join(root, "target")
        with open(target, "w", encoding="utf-8") as fh:
            fh.write("x")
        try:
            os.symlink(target, os.path.join(root, "link"))
        except (OSError, NotImplementedError):
            return False
    return True


def isolate_home(monkeypatch: Any, path: Any) -> None:
    """Point `~` at `path` for one test.

    Every variable that can carry it, because the platforms disagree: Windows
    resolves `os.path.expanduser("~")` from USERPROFILE, POSIX from HOME.
    Setting only HOME left Windows tests reading — and writing, e.g. fork copies
    under `~/.octopus/fork/` — the developer's real profile.
    """
    monkeypatch.setenv("HOME", str(path))
    monkeypatch.setenv("USERPROFILE", str(path))
