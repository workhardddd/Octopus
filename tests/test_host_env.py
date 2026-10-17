"""The environment a backend gets has to be *usable*, not just minimal.

`script_env` exists to keep the server's secrets out of app code (plan §4). That
part is pinned in `test_applications.py` and `test_app_agent.py`. This file pins
the other half, which failed for a real app: a backend that shells out to git
needs to know where the machine's home, system root and proxy are, and a
minimal POSIX-shaped environment tells it none of them.

Both failures below were measured on a real machine, one variable apart from a
working run:

* no `SystemRoot` → git's curl could not reach even a *loopback* proxy
  (`Failed to connect to 127.0.0.1 port 7891`);
* `HOME` pointing at a POSIX `/tmp` (the server's own value on Windows) → git
  never read `~/.gitconfig`, so it had no proxy at all and a `git fetch` hung on
  a direct connection to the git host instead of failing.
"""

from __future__ import annotations

import os

import pytest

from server.app_backends import host_env, script_env

WINDOWS = os.name == "nt"


def test_the_proxy_this_machine_uses_reaches_a_backend(monkeypatch):
    """A box that reaches the network through a proxy says so in its
    environment, and a backend that must make an outbound request has no other
    way to learn it — nor any way to report that it could not."""
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:7891")

    assert script_env("a1", "/apps/demo")["HTTPS_PROXY"] == "http://127.0.0.1:7891"


def test_only_the_proxy_names_are_carried_over(monkeypatch):
    """The whole point of `script_env` is that the *rest* of the server's
    environment does not travel. The proxy list is a whitelist, not a door."""
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:7891")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret")
    monkeypatch.setenv("OCTOPUS_AUTH_TOKEN", "super-secret")

    env = script_env("a1", "/apps/demo")

    assert env["HTTPS_PROXY"] == "http://127.0.0.1:7891"
    assert "ANTHROPIC_API_KEY" not in env
    assert "OCTOPUS_AUTH_TOKEN" not in env


@pytest.mark.skipif(not WINDOWS, reason="TEMP/TMP are how Windows names a temp dir")
def test_a_backend_is_told_where_it_may_write_scratch_files(monkeypatch, tmp_path):
    """Without a temp dir a tool falls back to its working directory — which for
    a backend is the app's code directory, the one Octopus publishes as static
    files.

    Windows-only on purpose: a POSIX host falls back to `/tmp`, which is a place
    a tool may actually use, so there is nothing to carry over.
    """
    monkeypatch.setenv("TEMP", str(tmp_path))
    monkeypatch.setenv("TMP", str(tmp_path))

    env = host_env()

    assert env.get("TEMP") == str(tmp_path)
    assert env.get("TMP") == str(tmp_path)


@pytest.mark.skipif(not WINDOWS, reason="USERPROFILE is how Windows names a home")
def test_windows_backends_learn_the_home_the_server_does_not_have(monkeypatch, tmp_path):
    """The measured failure: Octopus on Windows has no `HOME` of its own, so a
    backend was handed a POSIX `/tmp` — and git, which prefers `HOME` when it is
    set, looked for its config in a directory that means nothing there."""
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.delenv("HOME", raising=False)
    monkeypatch.setenv("SystemRoot", r"C:\Windows")
    monkeypatch.setenv("windir", r"C:\Windows")

    env = script_env("a1", "/apps/demo")

    assert env["HOME"] == str(tmp_path)
    assert env["USERPROFILE"] == str(tmp_path)
    assert env["SystemRoot"] == r"C:\Windows"
    assert env["windir"] == r"C:\Windows"


@pytest.mark.skipif(not WINDOWS, reason="the fallbacks below are Windows-shaped")
def test_windows_backends_can_still_find_their_system_root(monkeypatch):
    """`windir` alone is enough: the shell Git ships reads `%SystemRoot%`, and a
    missing value there is how a literal `%SystemDrive%` becomes a relative path
    inside the app directory."""
    monkeypatch.delenv("SystemRoot", raising=False)
    monkeypatch.setenv("windir", r"C:\Windows")

    env = host_env()

    assert env["SystemRoot"] == r"C:\Windows"


@pytest.mark.skipif(WINDOWS, reason="a POSIX host has a real HOME to pass on")
def test_a_posix_backend_keeps_the_servers_home(monkeypatch):
    monkeypatch.setenv("HOME", "/home/tester")

    assert script_env("a1", "/apps/demo")["HOME"] == "/home/tester"
