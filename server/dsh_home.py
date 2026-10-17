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
* **A persona is prose, not a template.** DSH hands the persona to the prompt
  registry as a *section*, and sections interpolate `{{name}}` references —
  while the loop registers exactly two variables, `provider` and `model`. So
  the patch emits no `personaSuffix` (the working directory arrives as literal
  runtime context anyway) and a persona containing a reference is refused at
  spawn instead of aborting prompt assembly inside the turn, which the engine
  can only report as a bare "Internal error".

The profile workspace is the exception to per-agent isolation: DSH materializes
a plugin workspace inside `$DSH_HOME/profiles/<name>` on first boot (slow, and
it needs the network), so one pre-warmed workspace is shared and symlinked into
every agent home.
"""

from __future__ import annotations

import logging
import os
import re
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
    # Mounting a plugin is another way to acquire a shell, so it is a
    # capability row like the rest. (`tool-ralph` is gone as of 0.2.1 — the
    # conformance test is what reports a row that stopped existing.)
    "tool-plugin-manager",
)

#: The provider route a bare model id belongs to. DSH's first-party route; any
#: other route is a `provider/model` pair (docs/plans/dsh-harness.md §3.8).
DEFAULT_PROVIDER = "deepseek-official"

#: The model Octopus asks DSH for when an agent names none.
#:
#: It has to be asked for explicitly. DSH's own ACP application ships a patch
#: pinning `deepseek-v4-flash` (`dsh-acp-app/cordis.patch.yml`), and `dsh-acp`
#: never consults `agent-default-model` — it uses the route it was configured
#: with, or the one the *client* selects over ACP. So without this, every
#: Octopus turn ran on a model nobody chose.
DEFAULT_MODEL = "deepseek-flash"

#: The model catalog every managed home declares, and the reason it exists.
#:
#: The DeepSeek provider plugin's `llm-deepseek.models` **replaces** its
#: built-in catalog (`z.array(catalogModel).default(DEFAULT_MODELS)`), so
#: declaring a list means restating every route DSH ships. The shipped
#: `deepseek-v4-flash` declares no image modality, and the ACP bridge refuses
#: image content outright for a model that does not (`dsh-acp`: "model … does
#: not declare image input"). `deepseek-flash` is declared `[text, image]` here,
#: which is what opens that gate — the same declaration the operator's own
#: `~/.dsh/settings.yaml` makes; without it a managed home gets the built-in
#: (text-only) view of the same model.
_SETTINGS_MODELS: tuple[dict[str, object], ...] = (
    {
        "id": "deepseek-flash",
        "name": "deepseek-flash",
        "contextWindow": 1_000_000,
        "inputModalities": ("text", "image"),
    },
    {"id": "deepseek-v4-flash", "name": "DeepSeek-V4-Flash"},
    {"id": "deepseek-v4-pro", "name": "DeepSeek-V4-Pro"},
    {
        "id": "deepseek-v4-flash-vision-exp",
        "name": "DeepSeek-V4-Flash-Vision-Exp",
        "inputModalities": ("text", "image"),
    },
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


def ensure_agent_home(agent_id: str, model: str | None = None) -> Path:
    """Create the agent's DSH home and everything DSH expects to find in it.

    Idempotent: called before every turn, so a home deleted out from under us
    (or never created, for an agent that predates this harness) is rebuilt.
    `model` is the agent's chosen route: a bare id DSH's shipped catalog does
    not list is declared here, because that catalog *replaces* the plugin's
    built-in one and an undeclared route is refused at selection
    (`dsh-acp`: "model … does not declare image input" — see
    `_catalog_for`).
    """
    home = agent_home(agent_id)
    home.mkdir(parents=True, exist_ok=True)
    write_settings(home, model)
    _link_shared_profiles(home)
    agent_memory.ensure_agent_dirs(agent_id)
    _link_memory_view(agent_id)
    return home


def ensure_oneshot_home(model: str | None = None) -> Path:
    """The home a one-shot with no owning agent runs in. Shared, because there
    is no agent to isolate it from and nothing here outlives the call."""
    home = oneshot_home()
    home.mkdir(parents=True, exist_ok=True)
    write_settings(home, model)
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
    """Remove one DSH session: its log directory and its cached projection.

    DSH has no deletion API — its own README says nothing deletes session
    files — so Octopus's hard session delete is the only thing that keeps the
    store from growing forever. Two artifacts belong to one conversation, and
    both are per-session files under the agent's home:

    * `<home>/sessions/--<cwd>--/<session id>/` — the transcript. The cwd
      component is a lossy encoding of a path we are not given here, so the id
      is what we search for.
    * `<home>/storages/session_projcache/sessions/<id>.json` — the derived
      projection the session list is built from. Deleting a cache costs, at
      worst, one recomputation; leaving it behind is a file per deleted session
      that nothing else would ever collect.
    """
    if not resume_id:
        return
    home = agent_home(agent_id)
    sessions = home / "sessions"
    if sessions.is_dir():
        for project in sessions.iterdir():
            candidate = project / resume_id
            if candidate.is_dir():
                shutil.rmtree(candidate, ignore_errors=True)
                logger.info(
                    "removed the DSH session store for %s (%s)", resume_id, candidate
                )
    cached = (
        home / "storages" / "session_projcache" / "sessions" / f"{resume_id}.json"
    )
    if cached.is_file():
        cached.unlink(missing_ok=True)
        logger.info("removed the cached DSH projection for %s (%s)", resume_id, cached)


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


#: The prompt variables DSH's loop registers, and therefore the whole universe a
#: persona could legally name (`dsh-system-prompt`: "The loop supplies `provider`
#: and `model`; plugins can register the facts they own" — and no plugin Octopus
#: mounts registers one).
_PROMPT_VARIABLES = ("provider", "model")

#: A prompt-variable reference: the `{{name}}` group `dsh-system-prompt`
#: interpolates. Matched as written (`[^{}]*` inside) because that is the
#: engine's own grammar — a group it cannot parse is a hard failure too, but
#: with a different message, and neither belongs in a persona.
_PROMPT_REFERENCE = re.compile(r"\{\{[^{}]*\}\}")


def _refuse_prompt_references(persona: str) -> None:
    """Refuse a persona, rather than a turn, that reads like a template.

    `dsh-persona` registers the persona as a prompt *section*, and sections
    interpolate `{{name}}` by default; the loop registers only `provider` and
    `model`. Anything else — an unknown name, or a group the grammar cannot
    parse — throws inside `renderPrompt`, i.e. before the model is asked
    anything, and the ACP client can only report it as `Internal error: turn
    failed: …`. Found for real: a `personaSuffix` we generated (“Your working
    directory is {{cwd}}.”) made *every* DSH turn fail the moment DSH 0.2.1
    stopped registering `cwd` (0.2.1-rc.2).

    Removing the braces from the user's text would silently edit what the model
    reads, and DSH has no escape syntax for an interpolated section
    (dsh-system-prompt README: "No inline escape syntax in interpolated text"),
    so the honest answer is to stop here, where the message reaches the user
    through the turn's own error bubble.
    """
    reference = _PROMPT_REFERENCE.search(persona or "")
    if reference is None:
        return
    raise ValueError(
        f"the agent's persona contains the prompt-variable reference "
        f"{reference.group(0)!r}. DSH delivers a persona as a prompt template "
        f"and resolves `{{{{name}}}}` references in it, but the loop registers "
        f"only {', '.join('{{' + name + '}}' for name in _PROMPT_VARIABLES)} — "
        f"so an unresolved reference aborts prompt assembly before the model is "
        f"asked anything. Octopus sends the persona as prose, so remove the "
        f"braces from it (DSH has no escape syntax for a literal group)."
    )


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
    _refuse_prompt_references(persona)
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
    # No `personaSuffix`, deliberately. It is a second interpolated section, so
    # it can only add ways to fail prompt assembly, and the one thing it used to
    # carry — "Your working directory is {{cwd}}." — is both unresolvable on
    # DSH 0.2.x (the loop registers `provider` and `model`, nothing else) and
    # redundant: `working-directory` supplies the cwd as literal runtime
    # context. The schema default for the field is the empty string, which is
    # exactly what leaving it out means.

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

    The text is rendered before anything is touched, so a spawn that is refused
    up front (`_refuse_prompt_references`) leaves the previous patch — and the
    directory — exactly as it was.
    """
    text = render_patch(
        persona=persona, memory_dir=memory_dir, web_research=web_research
    )
    directory = patch_path(agent_id, signature).parent
    directory.mkdir(parents=True, exist_ok=True)
    for stale in directory.glob("*.yml"):
        if stale.name != f"{signature}.yml":
            stale.unlink(missing_ok=True)
    path = patch_path(agent_id, signature)
    path.write_text(text, encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #


def _catalog_for(model: str | None) -> tuple[dict[str, object], ...]:
    """The shipped catalog, plus the agent's own route when it names one.

    A bare model id is selected as `[deepseek-official, id]` over ACP
    (`dsh_acp.model_option_value`), and the `deepseek-official` provider's
    catalog is exactly what `llm-deepseek.models` declares — so an agent's
    custom route has to be declared there or DSH refuses the selection. A
    `provider/model` pair names a different plugin's provider, whose catalog
    Octopus cannot speak for; it passes through untouched.

    A custom id is declared text-only: modalities Octopus cannot know must
    not claim image support, or the ACP bridge would let image content
    through to a model that cannot take it.
    """
    if not model or "/" in model:
        return _SETTINGS_MODELS
    if any(entry["id"] == model for entry in _SETTINGS_MODELS):
        return _SETTINGS_MODELS
    return (*_SETTINGS_MODELS, {"id": model, "name": model})


def render_settings(model: str | None = None) -> str:
    """The `settings.yaml` a managed home gets (see `_SETTINGS_MODELS`).

    Hand-rendered rather than dumped through a YAML writer, for the same reason
    the patch is: the file is small, fixed in shape, and reviewed as text.
    """
    lines = [
        "# Generated by Octopus — regenerate, do not hand-edit.",
        "llm-deepseek:",
        "  models:",
    ]
    for model_entry in _catalog_for(model):
        lines.append(f"    - id: {model_entry['id']}")
        lines.append(f"      name: {model_entry['name']}")
        if window := model_entry.get("contextWindow"):
            lines.append(f"      contextWindow: {window}")
        if modalities := model_entry.get("inputModalities"):
            lines.append(f"      inputModalities: [{', '.join(modalities)}]")
    lines += [
        "agent-default-model:",
        f"  provider: {DEFAULT_PROVIDER}",
        f"  model: {DEFAULT_MODEL}",
    ]
    return "\n".join(lines) + "\n"


def write_settings(home: Path, model: str | None = None) -> Path:
    """Write the home's `settings.yaml`, idempotently.

    Rewritten only when its content moved, so a running DSH never sees the file
    change under it for no reason.
    """
    path = home / "settings.yaml"
    text = render_settings(model)
    if not path.exists() or path.read_text(encoding="utf-8") != text:
        path.write_text(text, encoding="utf-8")
    return path
