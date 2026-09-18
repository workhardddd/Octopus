"""The generated patch, checked against the real CLI's composed tree.

`dsh --dump-config --profile acp --patch <ours>` prints the composed tree, so
this is the one test that sees whether the YAML Octopus writes is accepted by
the DSH version actually installed, and whether the rows it names still carry
the fields it restates.

Why that matters (docs/plans/dsh-harness.md §3.9): a DSH patch replaces the
*whole* config of every row it names, and the field names in it belong to DSH.
An upgrade that renames a field or adds one we would drop could otherwise
change what every Octopus agent runs under — a lost persona, an unpinned
permission posture — with the rest of the suite still green. Here it fails
loudly instead.

Gated on the CLI being installed; no credential is needed, because this never
runs a turn.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from server import dsh_home
from server.config import settings
from server.harness.run import _which_with_fallback
from tests.cli_gate import dsh_cli_present

pytestmark = pytest.mark.skipif(
    not dsh_cli_present(),
    reason="dsh CLI not on PATH; skipping the profile-conformance checks",
)


@pytest.fixture(autouse=True)
def _isolated_dsh_home(tmp_path, monkeypatch):
    """`write_patch` puts the file under the agent's DSH home; point that at a
    temp root so a test run never writes into the real `~/.octopus`."""
    monkeypatch.setattr(settings, "dsh_home_dir", str(tmp_path / "dsh"))
    monkeypatch.setattr(settings, "agents_dir", str(tmp_path / "agents"))

#: The rows the generated patch names, and the config keys each one must still
#: have in the shipped composition. A patch restates these fields; if DSH adds
#: another, this fails and someone decides whether to keep it.
_RESTATED_ROWS = {
    "system-prompt": {"personaPrefix", "personaSuffix"},
    "agent-instructions": {"maxBytes"},
    "sandbox-policy": {"mode", "workspaceRoot"},
    "approval": {"policy"},
    "permission": {"presets"},
}


def _dsh(*args: str) -> subprocess.CompletedProcess[str]:
    exe = _which_with_fallback("dsh")
    assert exe is not None, "the gate said dsh is present"
    return subprocess.run(
        [exe, *args],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        # Explicit: the composed tree is UTF-8, and a locale-encoded read
        # (`text=True` alone) dies on it — on Windows with GBK, which surfaces
        # as an empty stdout rather than a decode error.
        encoding="utf-8",
        errors="replace",
        timeout=180,
    )


def _dump(patch_path: str | None = None) -> str:
    args = ["--dump-config", "--profile", "acp"]
    if patch_path is not None:
        args += ["--patch", patch_path]
    proc = _dsh(*args)
    assert proc.returncode == 0, (
        f"dsh rejected the composed profile: rc={proc.returncode}\n"
        f"stderr: {proc.stderr[:800]}"
    )
    return proc.stdout


def _row_block(dumped: str, row_id: str) -> list[str]:
    """The lines of one row's LAST occurrence.

    The dump prints a section per applied layer, so the last time a row id
    appears is the configuration that actually takes effect. A row is its
    `- id:` line plus every following line until the next row or section.
    """
    lines = dumped.splitlines()
    start = None
    for index, line in enumerate(lines):
        if line.strip() == f"- id: {row_id}":
            start = index
    if start is None:
        return []
    block = [lines[start]]
    for line in lines[start + 1 :]:
        if line.startswith("- id:") or line.startswith("# =="):
            break
        block.append(line)
    return block


def _row_config_keys(dumped: str, row_id: str) -> set[str]:
    """The top-level keys of one row's `config:` block."""
    keys: set[str] = set()
    in_config = False
    for line in _row_block(dumped, row_id):
        if line == "  config:":
            in_config = True
            continue
        if not in_config:
            continue
        if line.startswith("      "):  # a nested value, not a top-level key
            continue
        if line.startswith("    "):
            keys.add(line.strip().split(":", 1)[0])
        elif line and not line.startswith(" "):
            break
    return keys


def _row_config_value(dumped: str, row_id: str, key: str) -> str | None:
    """One scalar value from a row's `config:` block, or None.

    Handles the two shapes DSH's YAML writer emits: a plain inline scalar, and
    a block scalar (`>-` / `|-`) — it picks the block form for values that would
    need quoting, such as a Windows path with a drive colon in it.
    """
    block = _row_block(dumped, row_id)
    prefix = f"    {key}:"
    for index, line in enumerate(block):
        if not line.startswith(prefix):
            continue
        inline = line[len(prefix) :].strip()
        if inline and inline not in (">", ">-", "|", "|-", ">+", "|+"):
            return inline
        body: list[str] = []
        for following in block[index + 1 :]:
            if following.strip() and not following.startswith("      "):
                break
            body.append(following.strip())
        return " ".join(body).strip()
    return None


def _row_is_disabled(dumped: str, row_id: str) -> bool:
    return any(line.strip() == "disabled: true" for line in _row_block(dumped, row_id))


def test_the_shipped_composition_still_has_the_fields_the_patch_restates(
    tmp_path,
):
    dumped = _dump()
    for row_id, expected in _RESTATED_ROWS.items():
        actual = _row_config_keys(dumped, row_id)
        assert actual == expected, (
            f"row {row_id!r} composes {sorted(actual)}, but the generated patch "
            f"restates {sorted(expected)}. A DSH upgrade changed the row's "
            f"fields: update server/dsh_home.py's render_patch (a patch replaces "
            f"the whole config, so anything not restated is dropped)."
        )


def test_a_generated_turn_patch_is_accepted_and_takes_effect(tmp_path):
    """The real acceptance test for our YAML: DSH parses it, validates every
    key against its schema, and the composed tree carries what we meant."""
    patch = dsh_home.write_patch(
        "agent-conformance",
        "sig-turn",
        persona="You are Octo.\nWith a colon: a # hash and 'quotes'.",
        memory_dir=str(tmp_path / "memory"),
        web_research=False,
    )
    dumped = _dump(str(patch))

    # The persona, and the cwd line the shipped profile puts after it.
    assert "You are Octo." in dumped
    assert "With a colon: a # hash and 'quotes'." in dumped
    assert _row_config_value(dumped, "system-prompt", "personaSuffix") == (
        "Your working directory is {{cwd}}."
    )
    # The posture is pinned rather than inferred.
    assert _row_config_value(dumped, "sandbox-policy", "mode") == "danger-full-access"
    assert _row_config_value(dumped, "approval", "policy") == "never"
    assert (
        _row_config_value(dumped, "permission", "defaultPreset")
        == "danger-full-access"
    )
    # …and the memory dir DSH will load instructions from. Compared as a path,
    # because the composed value is whatever the schema resolved the string to.
    memory = _row_config_value(dumped, "agent-instructions", "dshHome")
    assert memory is not None
    assert Path(memory) == tmp_path / "memory"


def test_a_generated_leaf_patch_disables_exactly_the_leaf_rows(tmp_path):
    """A research leaf searches the web and nothing else: ACP cannot set a
    per-turn tool policy, so the scoping lives in the patch, and this is where
    we find out whether the rows it turns off still exist."""
    patch = dsh_home.write_patch(
        "agent-conformance",
        "sig-leaf",
        persona="leaf",
        memory_dir=None,
        web_research=True,
    )
    dumped = _dump(str(patch))

    for row in dsh_home._LEAF_DISABLED_ROWS:
        assert _row_is_disabled(dumped, row), (
            f"the leaf patch turns off {row!r}, but the composed tree does not "
            f"show it disabled — the row was renamed or dropped"
        )
    # The web tools are what the leaf is for; they stay on.
    assert not _row_is_disabled(dumped, "tool-web")
    assert _row_config_value(dumped, "tool-web", "fetch") == "true"
    # No memory: a leaf has no agent-scoped memory to read, so the row keeps
    # the shipped config untouched.
    assert _row_config_keys(dumped, "agent-instructions") == {"maxBytes"}


def test_a_missing_patch_file_fails_the_boot(tmp_path):
    """DSH throws on a patch file it cannot read. Worth pinning: it is why the
    profile writes the patch before rendering argv (dsh.py `_prepare_spawn`),
    rather than passing a path it hopes exists."""
    proc = _dsh(
        "--dump-config", "--profile", "acp", "--patch", str(tmp_path / "nope.yml")
    )
    assert proc.returncode != 0
