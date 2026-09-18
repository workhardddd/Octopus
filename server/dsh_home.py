"""Per-agent DSH home, generated patch, memory view and store cleanup.

DSH reads one `DSH_HOME` per process, and that single directory carries its
sessions, settings, credentials, profiles and user-global instruction file. An
agent therefore gets one of its own, so Octopus's conversations never mix with
the owner's own `dsh` usage on the same machine, and so the memory DSH loads is
*that* agent's (docs/plans/dsh-harness.md §3.5).

Three things follow from DSH's own rules, verified against the installed CLI:

* **The patch is a pure function of the spawn.** DSH takes persona and
  permission posture by *composition*, never as a runtime argument, so each
  spawn writes `<agent_home>/patches/<signature>.yml` and passes it with
  `--patch`. A patch replaces the whole config of every row it names — it does
  not deep-merge — so this module restates each row's fields deliberately, and
  `tests/test_dsh_profile_conformance.py` asserts the composed tree still has
  them.
* **The posture is pinned, not inferred.** `dsh-permission-presets` infers a
  default preset from the composed sandbox/approval defaults; we name it
  explicitly so a DSH upgrade cannot quietly change what an Octopus turn runs
  under (docs/adr/0001-dsh-unfenced-execution.md).
* **The instruction file name is fixed.** DSH loads `<dshHome>/AGENTS.md` for
  the user-global scope (`instructionFileCandidates` only governs the project
  chain), so the canonical memory dir gets an `AGENTS.md` symlink to its
  `MEMORY.md`: one file under two names, which is what keeps "memory lives on
  the agent and survives an engine swap" true.

The profile workspace is the exception to per-agent isolation: DSH materializes
a plugin workspace inside `$DSH_HOME/profiles/<name>` on first boot (slow, and
it needs the network), so one pre-warmed workspace is shared and symlinked into
every agent home.
"""

from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path

from . import agent_memory
from .config import settings

logger = logging.getLogger(__name__)

#: DSH's own profiles Octopus uses: `acp` drives turns, `headless` one-shots.
DSH_PROFILES = ("acp", "headless")

#: The posture every Octopus DSH turn runs under. Unfenced on purpose — see
#: docs/adr/0001-dsh-unfenced-execution.md.
SANDBOX_MODE = "danger-full-access"
APPROVAL_POLICY = "never"
PERMISSION_PRESET = "danger-full-access"

#: DSH's shipped cap for the user-global instruction file. Restated because a
#: patch replaces the row's whole config.
INSTRUCTION_MAX_BYTES = 65536

#: The three presets `dsh-base` composes. Our patch names the `permission` row,
#: which means restating the whole table — a patch never deep-merges.
_SHIPPED_PRESETS: tuple[tuple[str, str, str], ...] = (
    ("read-only", "read-only", "ask"),
    ("workspace-write", "workspace-write", "ask"),
    ("danger-full-access", "danger-full-access", "never"),
)

#: Rows a deep-research web leaf turns off: it needs to search and read the web
#: and nothing else. ACP cannot set a per-turn tool policy, so scoping is a
#: spawn-level patch (docs/plans/dsh-harness.md §3.7).
#:
#: The sub-agent **tools** are here and the sub-agent **service** rows
#: (`subagent`, `subagent-spawn-in-process`, `subagent-fork-in-process`) are
#: deliberately not: the composed tree injects those, so disabling them fails
#: the boot — the ACP handshake comes back with a bare "Internal error" and the
#: CLI writes nothing to stderr, which is exactly what
#: `tests/test_backend_dsh_real.py::test_a_real_web_leaf_runs_scoped` caught.
#: Without the tools the model has nothing to fan out with, which is what the
#: leaf actually needs.
_LEAF_DISABLED_ROWS = (
    "tool-bash",
    "tool-pwsh",
    "tool-jobs",
    "tool-fs",
    "tool-fs-search",
    "tool-subagent",
    "tool-subagent-control",
    "tool-subagent-list-agents",
    "tool-subagent-fork",
    "tool-workflow",
    "tool-goal",
    "tool-ralph",
)


# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #


def dsh_root() -> Path:
    return Path(os.path.expanduser(settings.dsh_home_dir))


def agents_root() -> Path:
    return dsh_root() / "agents"


def agent_home(agent_id: str) -> Path:
    """`<dsh_home_dir>/agents/<agent_id>/` — one DSH_HOME per agent."""
    return agents_root() / agent_id


def shared_profiles_dir() -> Path:
    return dsh_root() / "profiles"


def oneshot_home() -> Path:
    """The home a one-shot with no owning agent runs in (a schedule parse from
    an agentless caller). Shared, because there is nothing to isolate."""
    return dsh_root() / "_oneshot"


def patch_path(agent_id: str, signature: str) -> Path:
    return agent_home(agent_id) / "patches" / f"{signature}.yml"


# --------------------------------------------------------------------------- #
# Provisioning
# --------------------------------------------------------------------------- #


def ensure_agent_home(agent_id: str) -> Path:
    """Create the agent's DSH home and everything DSH expects to find in it.

    Idempotent: called before every turn, so a home deleted out from under us
    (or never created, for an agent that predates this harness) is rebuilt.
    """
    home = agent_home(agent_id)
    home.mkdir(parents=True, exist_ok=True)
    _link_shared_profiles(home)
    agent_memory.ensure_agent_dirs(agent_id)
    _link_memory_view(agent_id)
    return home


def ensure_oneshot_home() -> Path:
    """The home a one-shot with no owning agent runs in. Shared, because there
    is no agent to isolate it from and nothing here outlives the call."""
    home = oneshot_home()
    home.mkdir(parents=True, exist_ok=True)
    _link_shared_profiles(home)
    return home


def _link_shared_profiles(home: Path) -> None:
    """Point this home's `profiles/` at the shared, pre-warmed workspace.

    Built once instead of once per agent because DSH materializes a plugin
    workspace on first boot. If the link cannot be created (a filesystem
    without symlink support) we say so and let DSH initialize its own copy in
    this home: slower and online, but correct.
    """
    link = home / "profiles"
    target = shared_profiles_dir()
    target.mkdir(parents=True, exist_ok=True)
    if link.is_symlink():
        if os.path.realpath(link) == os.path.realpath(target):
            return
        link.unlink()
    elif link.exists():
        return  # a real directory DSH already initialized here
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        logger.warning(
            "could not link %s to the shared DSH profiles at %s; DSH will "
            "initialize its own copy there (slower, needs the network)",
            link,
            target,
            exc_info=True,
        )


#: Prepended to the fallback memory view so the agent writes the canonical file
#: rather than the view it was handed. Only used where symlinks are impossible
#: (Windows without the create-symlink privilege); on a normal filesystem the
#: view *is* the canonical file and there is nothing to explain.
_MEMORY_VIEW_HEADER = (
    "<!-- Read-only view of MEMORY.md, regenerated by Octopus at every turn. -->\n"
    "<!-- Write your memory to MEMORY.md in this same directory; anything\n"
    "     written here is overwritten from it. -->\n"
)


def _link_memory_view(agent_id: str) -> None:
    """Give DSH the `AGENTS.md` it loads for the user-global scope, backed by
    the canonical `MEMORY.md` — one file under two names.

    DSH's user-global instruction file name is fixed (`USER_GLOBAL_FILE` in
    dsh-agent-instructions; `instructionFileCandidates` only governs the
    project chain), and the canonical file is Claude's `MEMORY.md`, so the two
    names have to meet. A symlink makes them the same file. Where symlinks are
    unavailable the view becomes a copy refreshed at every spawn, with a header
    telling the agent which file to write.
    """
    memory_dir = agent_memory.agent_memory_dir(agent_id)
    view = memory_dir / "AGENTS.md"
    canonical = memory_dir / "MEMORY.md"
    # The canonical file must exist before the view points at it, or the view
    # is a dangling symlink and DSH reads ENOENT for its instruction file.
    # An empty memory file is what an agent with no memory yet looks like.
    canonical.touch(exist_ok=True)
    if view.is_symlink():
        if os.path.realpath(view) == os.path.realpath(canonical):
            return
        view.unlink()
    elif view.exists():
        # A regular file: the fallback view, refreshed from the canonical file
        # on every spawn so the agent always reads current memory.
        _write_memory_view(agent_id)
        return
    try:
        view.symlink_to(canonical.name)
        return
    except OSError:
        logger.info(
            "no symlink support for %s; falling back to a refreshed copy of %s",
            view,
            canonical,
        )
    _write_memory_view(agent_id)


def _write_memory_view(agent_id: str) -> None:
    """The no-symlink fallback: a copy of the canonical memory, prefixed with
    the rule that keeps one source of truth."""
    memory_dir = agent_memory.agent_memory_dir(agent_id)
    canonical = memory_dir / "MEMORY.md"
    body = canonical.read_text(encoding="utf-8") if canonical.exists() else ""
    (memory_dir / "AGENTS.md").write_text(
        _MEMORY_VIEW_HEADER + body, encoding="utf-8"
    )


def ensure_shared_profiles() -> None:
    """Make sure the shared profile workspace exists. Called at boot so the
    first turn is not the thing that pays for it (and so a cold, network-bound
    initialization cannot be mistaken for a hung turn)."""
    shared_profiles_dir().mkdir(parents=True, exist_ok=True)


def remove_agent_home(agent_id: str) -> None:
    """Delete the agent's DSH home (its sessions and patches too). Called on
    hard agent delete; archiving keeps everything."""
    shutil.rmtree(agent_home(agent_id), ignore_errors=True)


def purge_session_store(agent_id: str, resume_id: str | None) -> None:
    """Remove one DSH session's log directory, if this agent has it.

    DSH has no deletion API — its own README says nothing deletes session
    files — so Octopus's hard session delete is the only thing that keeps the
    store from growing forever. Best-effort: the layout is
    `<home>/sessions/--<cwd>--/<session id>/`, the cwd component is a lossy
    encoding of a path we are not given here, so the id is what we search for.
    """
    if not resume_id:
        return
    sessions = agent_home(agent_id) / "sessions"
    if not sessions.is_dir():
        return
    for project in sessions.iterdir():
        candidate = project / resume_id
        if candidate.is_dir():
            shutil.rmtree(candidate, ignore_errors=True)
            logger.info(
                "removed the DSH session store for %s (%s)", resume_id, candidate
            )


# --------------------------------------------------------------------------- #
# The generated patch
# --------------------------------------------------------------------------- #


def _block_scalar(text: str, indent: int) -> str:
    """Render `text` as a YAML literal block scalar body.

    Chosen over a quoted scalar because a persona is arbitrary prose: it may
    contain quotes, colons, `#`, backslashes and newlines, none of which a
    literal block interprets.
    """
    pad = " " * indent
    lines = text.splitlines() or [""]
    return "\n".join(f"{pad}{line}" if line else "" for line in lines)


def render_patch(
    *,
    persona: str,
    memory_dir: str | None,
    web_research: bool = False,
) -> str:
    """The YAML patch for one spawn.

    Every row named here has its whole config restated, because that is what a
    patch does — a partial one would silently drop the field it left out.
    """
    lines: list[str] = [
        "# Generated by Octopus for one spawn. Regenerate, do not hand-edit:",
        "# a DSH patch replaces the whole config of every row it names.",
        "",
        "- id: system-prompt",
        "  config:",
    ]
    if persona:
        lines.append("    personaPrefix: |-")
        lines.append(_block_scalar(persona, 6))
    else:
        lines.append('    personaPrefix: ""')
    # Kept from the shipped `acp` profile: the model is told where it is.
    lines.append("    personaSuffix: Your working directory is {{cwd}}.")

    lines += ["", "- id: approval", "  config:", f"    policy: {APPROVAL_POLICY}"]

    lines += [
        "",
        "- id: sandbox-policy",
        "  config:",
        f"    mode: {SANDBOX_MODE}",
        "    workspaceRoot: !!js process.cwd()",
    ]

    lines += [
        "",
        "- id: permission",
        "  config:",
        f"    defaultPreset: {PERMISSION_PRESET}",
        "    presets:",
    ]
    for name, sandbox, approval in _SHIPPED_PRESETS:
        lines += [
            f"      {name}:",
            f"        sandbox: {sandbox}",
            f"        approval: {approval}",
        ]

    if memory_dir:
        # DSH loads `<dshHome>/AGENTS.md` for the user-global scope, and that
        # symlink lives in the canonical memory dir (see `_link_memory_view`).
        lines += [
            "",
            "- id: agent-instructions",
            "  config:",
            f"    dshHome: {memory_dir}",
            f"    maxBytes: {INSTRUCTION_MAX_BYTES}",
        ]

    if web_research:
        lines += ["", "# A research leaf searches the web and nothing else."]
        for row in _LEAF_DISABLED_ROWS:
            lines += ["", f"- id: {row}", "  disabled: true"]

    return "\n".join(lines) + "\n"


def write_patch(
    agent_id: str,
    signature: str,
    *,
    persona: str,
    memory_dir: str | None,
    web_research: bool = False,
) -> Path:
    """Write this spawn's patch, replacing any earlier one for the same agent.

    One file per spawn signature, and stale ones are swept: the patch is
    regenerated whenever anything baked into it changes, so keeping old copies
    would only accumulate persona text on disk.
    """
    directory = patch_path(agent_id, signature).parent
    directory.mkdir(parents=True, exist_ok=True)
    for stale in directory.glob("*.yml"):
        if stale.name != f"{signature}.yml":
            stale.unlink(missing_ok=True)
    path = patch_path(agent_id, signature)
    path.write_text(
        render_patch(
            persona=persona, memory_dir=memory_dir, web_research=web_research
        ),
        encoding="utf-8",
    )
    return path
