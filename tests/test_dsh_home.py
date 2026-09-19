"""Per-agent DSH home provisioning and the generated patch (plan §3.5).

Everything here is about DSH's own rules: one `DSH_HOME` per process (sessions,
settings, credentials, profiles and its user-global instruction file all live
under it), a patch that replaces a row's whole config, and a fixed
`AGENTS.md` instruction-file name.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from server import dsh_home
from server.config import settings

AGENT = "agent-1"


@pytest.fixture(autouse=True)
def _isolated_roots(tmp_path, monkeypatch):
    """Both roots, because the DSH home points at the canonical memory dir and
    a test that overrode only one would write into the real `~/.octopus`."""
    monkeypatch.setattr(settings, "dsh_home_dir", str(tmp_path / "dsh"))
    monkeypatch.setattr(settings, "agents_dir", str(tmp_path / "agents"))


def _memory_dir() -> Path:
    return Path(settings.agents_dir) / AGENT / "memory"


# --------------------------------------------------------------------------- #
# Settings (the model route + its modalities)
# --------------------------------------------------------------------------- #


def test_settings_declare_the_model_route_octopus_selects():
    """The home says which model DSH should run, and that it takes images.

    Both halves are load-bearing: DSH's own ACP profile pins a *different*
    model, and the ACP bridge refuses image content unless the resolved catalog
    entry declares the modality — which is only true of what this file says.
    """
    home = dsh_home.ensure_agent_home(AGENT)
    text = (home / "settings.yaml").read_text(encoding="utf-8")

    assert f"model: {dsh_home.DEFAULT_MODEL}" in text
    assert f"provider: {dsh_home.DEFAULT_PROVIDER}" in text
    assert "id: deepseek-flash" in text
    # `deepseek-flash` is the one Octopus selects, so its modalities are the
    # ones that matter; the shipped v4 entries stay listed because the provider
    # plugin's model list REPLACES its built-in catalog.
    flash = text.split("- id: deepseek-flash", 1)[1].split("- id:", 1)[0]
    assert "inputModalities: [text, image]" in flash
    for shipped in ("deepseek-v4-flash", "deepseek-v4-pro"):
        assert f"id: {shipped}" in text


def test_settings_land_in_both_kinds_of_home_and_are_stable():
    """The one-shot home needs them too (it has no agent to inherit from), and
    a rewrite must be a no-op so a running DSH never sees the file churn."""
    agent_home = dsh_home.ensure_agent_home(AGENT)
    one_shot = dsh_home.ensure_oneshot_home()
    for home in (agent_home, one_shot):
        settings_file = home / "settings.yaml"
        assert settings_file.is_file()
        first = settings_file.stat().st_mtime_ns
        dsh_home.write_settings(home)
        assert settings_file.stat().st_mtime_ns == first


# --------------------------------------------------------------------------- #
# Layout
# --------------------------------------------------------------------------- #


def test_ensure_agent_home_creates_the_home_and_points_profiles_at_the_shared_one():
    home = dsh_home.ensure_agent_home(AGENT)
    assert home == Path(settings.dsh_home_dir) / "agents" / AGENT
    assert home.is_dir()
    # The profile workspace is shared (DSH materializes it on first boot, which
    # is slow and needs the network), so the home links to it where the
    # filesystem can, and otherwise lets DSH initialize its own copy.
    link = home / "profiles"
    if link.is_symlink():
        assert link.resolve() == dsh_home.shared_profiles_dir().resolve()
    else:
        assert not link.exists()


def test_ensure_agent_home_is_idempotent():
    first = dsh_home.ensure_agent_home(AGENT)
    second = dsh_home.ensure_agent_home(AGENT)
    assert first == second
    assert dsh_home.shared_profiles_dir().is_dir()


def test_memory_view_puts_dsh_instruction_file_name_on_the_canonical_memory():
    """DSH loads `<dshHome>/AGENTS.md` for the user-global scope and the
    canonical memory file is `MEMORY.md`, so the two names have to meet — as
    one file where symlinks exist, as a refreshed copy where they do not."""
    dsh_home.ensure_agent_home(AGENT)
    view = _memory_dir() / "AGENTS.md"
    canonical = _memory_dir() / "MEMORY.md"
    assert view.exists()

    canonical.write_text("# Memory\n\nRemember the thing.\n", encoding="utf-8")
    dsh_home.ensure_agent_home(AGENT)  # refreshed on the next spawn

    body = view.read_text(encoding="utf-8")
    assert "Remember the thing." in body
    if view.is_symlink():
        assert view.resolve() == canonical.resolve()
    else:
        # The copy says which file to write, so the two never diverge by accident.
        assert body.startswith(dsh_home._MEMORY_VIEW_HEADER)


def test_remove_agent_home_takes_the_sessions_with_it():
    home = dsh_home.ensure_agent_home(AGENT)
    (home / "sessions" / "--tmp--" / "sess-1").mkdir(parents=True)
    dsh_home.remove_agent_home(AGENT)
    assert not home.exists()


def test_purge_session_store_removes_only_that_session():
    """DSH has no deletion API, so Octopus's hard session delete is the only
    thing keeping its store from growing forever. The cwd component of the
    path is a lossy encoding we are not given, so the id is what we match."""
    home = dsh_home.ensure_agent_home(AGENT)
    doomed = home / "sessions" / "--some-project--" / "sess-doomed"
    kept = home / "sessions" / "--other-project--" / "sess-kept"
    doomed.mkdir(parents=True)
    kept.mkdir(parents=True)

    dsh_home.purge_session_store(AGENT, "sess-doomed")

    assert not doomed.exists()
    assert kept.exists()


def test_purge_session_store_is_inert_without_an_id_or_a_store():
    dsh_home.purge_session_store(AGENT, None)  # no resume id yet
    dsh_home.purge_session_store("agent-with-no-home", "sess-1")  # nothing there


# --------------------------------------------------------------------------- #
# The generated patch
# --------------------------------------------------------------------------- #


def test_patch_pins_persona_posture_and_memory_together():
    """A DSH patch replaces the whole config of every row it names, so each row
    here restates what it means to keep — the persona template keeps its cwd
    suffix, and the posture is pinned rather than inferred from the composed
    defaults."""
    yaml_text = dsh_home.render_patch(
        persona="You are Octo.\nLine two.", memory_dir="/mem", web_research=False
    )

    assert "- id: system-prompt" in yaml_text
    assert "personaPrefix: |-" in yaml_text
    assert "      You are Octo.\n      Line two." in yaml_text
    assert "personaSuffix: Your working directory is {{cwd}}." in yaml_text

    assert "policy: never" in yaml_text
    assert "mode: danger-full-access" in yaml_text
    assert "defaultPreset: danger-full-access" in yaml_text
    # The whole presets table is restated: a patch never deep-merges, so naming
    # the row without it would delete the presets DSH ships.
    for preset in ("read-only", "workspace-write", "danger-full-access"):
        assert f"      {preset}:" in yaml_text

    assert "dshHome: /mem" in yaml_text
    assert f"maxBytes: {dsh_home.INSTRUCTION_MAX_BYTES}" in yaml_text
    # A normal turn keeps every tool: only a research leaf scopes them.
    assert "disabled: true" not in yaml_text


def test_patch_scopes_a_research_leaf_to_the_web():
    yaml_text = dsh_home.render_patch(
        persona="leaf", memory_dir=None, web_research=True
    )
    for row in ("tool-bash", "tool-pwsh", "tool-fs", "tool-subagent", "tool-workflow"):
        assert f"- id: {row}\n  disabled: true" in yaml_text
    # A leaf reads no memory, so the instruction row is left out entirely.
    assert "agent-instructions" not in yaml_text


def test_patch_handles_an_empty_persona():
    """An agent with no system prompt still needs a valid patch: an empty
    literal block scalar would not parse."""
    yaml_text = dsh_home.render_patch(persona="", memory_dir=None)
    assert 'personaPrefix: ""' in yaml_text
    assert "|-" not in yaml_text


def test_write_patch_sweeps_the_previous_spawns_patch():
    """The patch is regenerated whenever anything baked into it changes, so an
    old copy is dead weight (and persona text) on disk."""
    first = dsh_home.write_patch(AGENT, "sig-one", persona="P", memory_dir=None)
    assert first.exists()
    second = dsh_home.write_patch(AGENT, "sig-two", persona="P2", memory_dir=None)

    assert second.exists()
    assert not first.exists()
    assert second.read_text(encoding="utf-8").count("P2") == 1
