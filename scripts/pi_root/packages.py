"""Pi packages: identity, the packages this repository owns, and the install step that also brings the tools they need."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from . import external, resources, unattended
from .context import Context, State
from .jsonfile import read_json, write_json
from .paths import (
    ANCHORLOOP_ROOT_VARIABLE,
    HERDR_AGENTS_ROOT_VARIABLE,
    LOCAL_PACKAGE,
    optional_plugins,
    overlay_root,
)
from .pins import (
    ANCHORLOOP_PACKAGE,
    CLAUDE_HOOKS_EXTENSION,
    CLAUDE_RULES_SOURCE,
    COMPUTER_USE_ID,
    HERDR_AGENTS_SOURCE,
    HERDR_PLUGINS,
    PACKAGES,
    PINNED_GIT,
    PLAYWRITER,
    RETIRED_SOURCES,
)

Package = str | dict


def package_source(value: Package) -> str:
    return value["source"] if isinstance(value, dict) else value


def package_id(value: Package, agent_dir: Path) -> str:
    source = package_source(value)
    if source.startswith("npm:"):
        # Pi identifies npm packages by name, so a pinned spec replaces its unpinned predecessor.
        spec = source.removeprefix("npm:")
        return "npm:" + spec[:spec.find("@", 1)] if spec.find("@", 1) > 0 else source
    if source.startswith("git:"):
        return source
    return str((agent_dir / source).resolve())


def unique_packages(values: list[Package], agent_dir: Path) -> list[Package]:
    seen = set()
    result = []
    for value in values:
        identity = package_id(value, agent_dir)
        if identity not in seen:
            seen.add(identity)
            result.append(value)
    return result


def obsolete_pin(value: Package) -> bool:
    source = package_source(value)
    return (any(source.startswith(prefixes) and source != current for current, prefixes in PINNED_GIT.items())
            or source.startswith(RETIRED_SOURCES))


def remove_retired_installs(agent_dir: Path, home: Path) -> None:
    """Take retired packages out of pi's npm and git roots: retiring one drops it from settings, after which `pi remove` no longer finds it."""
    npm_root = agent_dir / "npm"
    installed = read_json(npm_root / "package.json").get("dependencies", {})
    for source in RETIRED_SOURCES:
        if source.startswith("npm:"):
            spec = source.removeprefix("npm:")
            name = spec[:spec.find("@", 1)] if spec.find("@", 1) > 0 else spec
            if name in installed:
                # pi uninstalls with --legacy-peer-deps too; without it a stale peer range elsewhere fails the uninstall.
                external.run(["npm", "uninstall", name, "--prefix", str(npm_root), "--legacy-peer-deps"], home)
        elif (checkout := agent_dir / "git" / source.removeprefix("git:").split("@")[0]).is_dir():
            shutil.rmtree(checkout)


def anchorloop_source() -> str:
    """The pinned anchorloop release, or the checkout PI_ANCHORLOOP_ROOT names for testing."""
    value = os.environ.get(ANCHORLOOP_ROOT_VARIABLE)
    if not value:
        return ANCHORLOOP_PACKAGE
    root = Path(value).expanduser().resolve()
    if not (root / "package.json").is_file():
        raise ValueError(f"{ANCHORLOOP_ROOT_VARIABLE}={value} has no package.json; point it at a checkout or unset it to use {ANCHORLOOP_PACKAGE}")
    return str(root)



def plugin_source(name: str, variable: str, default: Path) -> str | None:
    root = Path(os.environ.get(variable, default)).resolve()
    if (root / "package.json").is_file():
        return str(root)
    print(f"{name} not found at {root}; skipped. Set {variable} to the plugin checkout and rerun to add it.", file=sys.stderr)
    return None


def herdr_agents_root() -> str | None:
    """The checkout named by PI_HERDR_AGENTS_ROOT, or None when the variable is unset; a wrong value fails the install."""
    value = os.environ.get(HERDR_AGENTS_ROOT_VARIABLE)
    if not value:
        return None
    root = Path(value).expanduser().resolve()
    manifest = root / "package.json"
    if not manifest.is_file():
        raise ValueError(f"{HERDR_AGENTS_ROOT_VARIABLE}={value} has no package.json at {manifest}; "
                         f"point it at a pi-herdr-agents checkout or unset it to use {HERDR_AGENTS_SOURCE}")
    name = read_json(manifest).get("name")
    if name != "pi-herdr-agents":
        raise ValueError(f"{HERDR_AGENTS_ROOT_VARIABLE}={value} is the package {name!r}, not pi-herdr-agents")
    return str(root)


def owned_packages(state: State) -> list[Package]:
    """PACKAGES minus the recorded profile's drops, with the pinned pi-herdr-agents release swapped for the override checkout the install recorded."""
    dropped = {package_id(package, Path()) for package in profile_dropped(state)}
    result = []
    for package in PACKAGES:
        if package_id(package, Path()) in dropped:
            continue
        if package == HERDR_AGENTS_SOURCE and state.get("herdr_agents"):
            package = state["herdr_agents"]
        elif package_source(package) == CLAUDE_RULES_SOURCE and state.get("pi_code_hooks") == "enabled":
            package = {**package, "extensions": [*package["extensions"], CLAUDE_HOOKS_EXTENSION]}
        result.append(package)
    return result


def profile_dropped(state: State) -> list[Package]:
    """The PACKAGES entries the recorded profile leaves out."""
    dropped = unattended.dropped_ids(state.get("profile"))
    if state.get("computer_use") == "disabled":
        dropped = dropped | {COMPUTER_USE_ID}
    return [package for package in PACKAGES if package_id(package, Path()) in dropped]


def plugin_keys(state: State) -> list[str]:
    """The overlay's optional plugins, including those the install recorded from an overlay that is gone now."""
    return list(dict.fromkeys([*state.get("overlay_plugins", []), *optional_plugins()]))


def local_packages(state: State) -> list[Package]:
    return [state.get("anchorloop", ANCHORLOOP_PACKAGE), LOCAL_PACKAGE, *(state[key] for key in plugin_keys(state) if state.get(key))]


def plan(ctx: Context) -> None:
    """Record which package sources this run uses and what settings held before the first install."""
    state = ctx.state
    herdr_root = ctx.herdr_root
    # A source that changed form (checkout to npm or back, or the git install aaaav had before 1.0, or pi-aaaav before its rename) leaves settings like a displaced pi-herdr-agents root;
    # so does a straw-boss checkout from before Straw Boss moved to the pi-straw-boss npm package.
    sources = {"anchorloop": anchorloop_source()}
    state["latest_displaced"] = [old for old in dict.fromkeys([*state.get("latest_displaced", []), state.pop("straw_boss", None), state.pop("aaaav", None),
                                 *(state.get(key) for key in sources)]) if old and old not in sources.values()]
    state.update(sources)
    # pi identifies an npm package by name but a local one by path, so whichever form the other mode used must leave settings.
    # Old checkouts stay recorded until a run completes, so a run that fails midway still removes them on the next one.
    state["herdr_agents_displaced"] = [root for root in dict.fromkeys([*state.get("herdr_agents_displaced", []), state.get("herdr_agents")])
                                       if root and root != herdr_root]
    if herdr_root:
        state["herdr_agents"] = herdr_root
        print(f"{HERDR_AGENTS_ROOT_VARIABLE}: using {herdr_root} instead of {HERDR_AGENTS_SOURCE}. "
              "Roles still come from pi-herdr-roles and the agents/ overrides.", file=sys.stderr)
    else:
        state.pop("herdr_agents", None)
    declared = optional_plugins()
    for key in plugin_keys(state):
        if key in declared and (source := plugin_source(*declared[key])):
            state[key] = source
        elif key in state:
            ctx.gone_plugins.append(state.pop(key))
    state["overlay_plugins"] = list(declared)
    settings = read_json(ctx.settings_path)
    state["previous_packages"] = state.get("previous_packages", list(settings.get("packages", [])))
    state.setdefault("retired_packages", [package for package in settings.get("packages", []) if obsolete_pin(package)])
    state.setdefault("integration_installed", not ctx.first_install)


# herdr web ui was the remote control until it was retired; an install that added the plugin removes it once.
HERDR_WEB_UI_ID = "devswha.herdr-web-ui"


def retire_herdr_web_ui(ctx: Context) -> None:
    if "herdr_web_ui_preinstalled" not in ctx.state:
        return
    # `herdr plugin uninstall` fails on a plugin that is not there, which an install with a failed build left behind.
    if not ctx.state["herdr_web_ui_preinstalled"] and HERDR_WEB_UI_ID in external.capture(["herdr", "plugin", "list"], ctx.home):
        external.run(["herdr", "plugin", "uninstall", HERDR_WEB_UI_ID], ctx.home)
    ctx.state.pop("herdr_web_ui_preinstalled")
    ctx.save()


def plugin_line(home: Path, plugin_id: str) -> str | None:
    """A plugin's line in `herdr plugin list`, or None when it is not installed."""
    listed = external.capture(["herdr", "plugin", "list"], home)
    # One id can begin another (straw-boss, straw-boss-reaper), and each plugin's config line names its id too.
    return next((line for line in listed.splitlines() if f"{plugin_id} (" in line), None)


def needs_portable_plugins() -> bool:
    """NixOS supplies bash on PATH, not at the upstream manifests' /bin/bash."""
    return not Path("/bin/bash").is_file()


# A NixOS system profile path survives garbage collection; a /nix/store bash on PATH does not.
STABLE_BASH_PATHS = ("/run/current-system/sw/bin/bash", "/usr/bin/bash")
PORTABLE_PLUGINS_DIR = Path(".local/share/weihung-agent-root/herdr-plugins")


def portable_bash() -> str:
    """The bash the prepared manifests run: a stable system path, or a PATH bash outside the Nix store."""
    stable = next((path for path in STABLE_BASH_PATHS if Path(path).is_file()), None)
    if stable:
        return stable
    bash = shutil.which("bash")
    if not bash:
        raise ValueError("bash is not on PATH; the Herdr plugins require bash")
    if bash.startswith("/nix/store/"):
        raise ValueError(f"bash resolves to {bash}, which Nix garbage collection removes; "
                         "run the installer where /run/current-system/sw/bin/bash exists")
    return bash


def manifest_interpreters(root: Path) -> set[str]:
    """The non-node interpreters a prepared manifest launches."""
    import tomllib
    manifest = tomllib.loads((root / "herdr-plugin.toml").read_text())
    return {entry["command"][0] for section in ("startup", "events", "actions")
            for entry in manifest.get(section, []) if entry["command"][0] != "node"}


def remove_portable_plugin(home: Path, plugin_id: str) -> None:
    shutil.rmtree(home / PORTABLE_PLUGINS_DIR / plugin_id, ignore_errors=True)


def portable_plugin_root(home: Path, plugin_id: str, repo: str, ref: str) -> Path:
    """Prepare a pinned local plugin before linking it; never edit Herdr's internal store."""
    bash = portable_bash()
    root = home / PORTABLE_PLUGINS_DIR / plugin_id / ref
    # An earlier run may have recorded a bash that is gone or differs now; prepare the pin again.
    if root.exists() and manifest_interpreters(root) != {bash}:
        shutil.rmtree(root)
    if root.parent.exists():
        # Older pins and the staging folders of an interrupted run.
        for sibling in root.parent.iterdir():
            if sibling.name != ref:
                shutil.rmtree(sibling)
    if not root.exists():
        root.parent.mkdir(parents=True, exist_ok=True)
        # A failed clone or patch leaves no final directory, so the next run retries.
        with tempfile.TemporaryDirectory(dir=root.parent) as directory:
            staging = Path(directory)
            checkout = staging / "checkout"
            owner, repository, subdir = repo.split("/", 2)
            external.run(["git", "clone", "--depth", "1", "--branch", ref,
                          f"https://github.com/{owner}/{repository}.git", str(checkout)], home)
            prepared = staging / "plugin"
            shutil.copytree(checkout / subdir, prepared)
            manifest = prepared / "herdr-plugin.toml"
            text = manifest.read_text()
            if '["/bin/bash",' not in text:
                raise ValueError(f"{repo}@{ref}: expected /bin/bash manifest commands; inspect the new pin")
            manifest.write_text(text.replace('["/bin/bash",', f'[{json.dumps(bash)},'))
            prepared.rename(root)
    return root


def install_herdr_plugins(ctx: Context) -> None:
    """Install pinned plugins, using portable local manifests only where /bin/bash is absent."""
    for plugin_id, repo, ref, preinstalled in HERDR_PLUGINS:
        line = plugin_line(ctx.home, plugin_id)
        if preinstalled not in ctx.state:
            ctx.state[preinstalled] = line is not None
            ctx.save()
        root = portable_plugin_root(ctx.home, plugin_id, repo, ref) if needs_portable_plugins() else None
        # `herdr plugin list` shows a linked plugin by its resolved path.
        expected = str(root.resolve()) if root else f"github:{repo}@{ref}"
        if line and expected in line:
            continue
        if line:
            external.run(["herdr", "plugin", "uninstall", plugin_id], ctx.home)
        if root:
            external.run(["herdr", "plugin", "link", str(root)], ctx.home)
        else:
            external.run(["herdr", "plugin", "install", repo, "--ref", ref, "--yes"], ctx.home)
    print("Herdr keys unchanged. Sidebar action: wayne930242.straw-boss.net; "
          "workspace pin action: wayne930242.straw-boss-reaper.pin. "
          "Pinned workspaces are kept by the reaper; see docs/reference.md.")


def apply(ctx: Context) -> None:
    """Install pi, its tools, and every owned package, then write the package list in the order pi expects."""
    home, state, agent_dir, settings_path = ctx.home, ctx.state, ctx.agent_dir, ctx.settings_path
    old_roots = state["herdr_agents_displaced"] + state["latest_displaced"]
    # The other form of pi-herdr-agents leaves settings even when the user had declared it first.
    retired_ids = {package_id(value, agent_dir) for value in old_roots + ([HERDR_AGENTS_SOURCE] if ctx.herdr_root else [])}
    # An overlay checkout is not a pi package; a machine whose settings still list it from before the overlay split sheds it.
    if overlay := overlay_root():
        retired_ids.add(package_id(str(overlay), agent_dir))
    # An install profile's drops leave settings like any retired package, for this run only: the default profile installs them again.
    retired_ids |= {package_id(package, agent_dir) for package in profile_dropped(state)}
    headless = unattended.is_unattended(ctx.profile)
    pinned = owned_packages(state)
    if not ctx.skip_external:
        external.require("herdr", "install herdr first; the pi integration, the Straw Boss plugins and the agents all need it")
        external.run(["npm", "install", "-g", "@earendil-works/pi-coding-agent@latest"], home)
        if not headless and "playwriter_preinstalled" not in state:
            listed = subprocess.run(["npm", "ls", "-g", "--depth=0", "playwriter"], capture_output=True, check=False)
            state["playwriter_preinstalled"] = listed.returncode == 0
            ctx.save()
        if not headless:
            external.run(["npm", "install", "-g", PLAYWRITER], home)
        external.run(["herdr", "integration", "install", "pi"], home)
        state["integration_installed"] = True
        ctx.save()
        retire_herdr_web_ui(ctx)
        install_herdr_plugins(ctx)
        for package in pinned:
            external.run(["pi", "install", package_source(package)], home)
        for package in state["retired_packages"]:
            settings = read_json(settings_path)
            if package not in settings.get("packages", []):
                continue
            if package_source(package).startswith("git:"):
                settings["packages"] = [value for value in settings["packages"] if value != package]
                write_json(settings_path, settings)
            else:
                external.run(["pi", "remove", package_source(package)], home)
        for package in read_json(settings_path).get("packages", []):
            if package_id(package, agent_dir) in retired_ids:
                external.run(["pi", "remove", package_source(package)], home)
        remove_retired_installs(agent_dir, home)
        external.run(["pi", "install", state["anchorloop"]], home)
        if headless:
            # The pi-skills clone and the statechart skill (needs Java) are desktop-side tools; a switch from the default
            # profile takes them back out. Linear's ttt stays: agents there use it in a project where a person ran
            # `ttt init` with their own key.
            resources.uninstall_pi_skills(home, state)
            resources.uninstall_statechart(home, state)
            resources.install_ttt(home, state, ctx.force)
        else:
            resources.install_ported_resources(home, state, ctx.force)
        ctx.save()
        external.run(["pi", "install", LOCAL_PACKAGE], home)
        for key in plugin_keys(state):
            if state.get(key):
                external.run_optional_plugin("install", home, state[key])
    else:
        settings = read_json(settings_path)
        settings["packages"] = [package for package in unique_packages(settings.get("packages", []) + pinned + local_packages(state), agent_dir)
                                if not obsolete_pin(package)]
        write_json(settings_path, settings)
    settings = read_json(settings_path)
    current = settings.get("packages", [])
    owned = pinned + local_packages(state)
    owned_ids = {package_id(value, agent_dir) for value in owned}
    unmanaged = [value for value in current
                 if package_id(value, agent_dir) not in owned_ids | retired_ids and not obsolete_pin(value)]
    # Registry and git specs are written as declared; `pi install` records a local path relative to the agent directory.
    declared = [source if package_source(source).startswith(("npm:", "git:")) else
                next((value for value in current if package_id(value, agent_dir) == package_id(source, agent_dir)), source)
                for source in owned]
    gone_ids = {package_id(value, agent_dir) for value in ctx.gone_plugins}
    settings["packages"] = [value for value in unique_packages(unmanaged + declared, agent_dir)
                            if package_id(value, agent_dir) not in gone_ids]
    write_json(settings_path, settings)
    state.pop("herdr_agents_displaced")
    state.pop("latest_displaced")


def restore(ctx: Context) -> None:
    """Remove what install added and put back the packages and tools the user had before it."""
    home, state, agent_dir, settings_path = ctx.home, ctx.state, ctx.agent_dir, ctx.settings_path
    resources.uninstall_ported_resources(home, state)
    # An explicit safety opt-out also applies to uninstall: never resurrect a disabled package.
    previous_packages = [package for package in state.get("previous_packages", [])
                         if not (state.get("computer_use") == "disabled" and package_id(package, agent_dir) == COMPUTER_USE_ID)]
    previous_ids = {package_id(value, agent_dir) for value in previous_packages}
    managed_packages = owned_packages(state) + local_packages(state)
    optional = [state[key] for key in plugin_keys(state) if state.get(key)]
    owned = {package_id(value, agent_dir) for value in managed_packages} - previous_ids
    if not ctx.skip_external:
        installed_ids = {package_id(value, agent_dir) for value in read_json(settings_path).get("packages", [])}
        for package in managed_packages:
            identity = package_id(package, agent_dir)
            if identity in owned and identity in installed_ids:
                if package in optional:
                    external.run_optional_plugin("remove", home, package_source(package))
                else:
                    external.run(["pi", "remove", package_source(package)], home)
        if state.get("integration_installed", True):
            external.run(["herdr", "integration", "uninstall", "pi"], home)
        retire_herdr_web_ui(ctx)
        # A plugin the user had before install stays; one install added goes.
        for plugin_id, _, _, preinstalled in HERDR_PLUGINS:
            if not state.get(preinstalled, True):
                if plugin_line(home, plugin_id):
                    external.run(["herdr", "plugin", "uninstall", plugin_id], home)
                remove_portable_plugin(home, plugin_id)
        if not state.get("playwriter_preinstalled", True):
            external.run(["npm", "uninstall", "-g", "playwriter"], home)
    settings = read_json(settings_path)
    # A package the user declared before install keeps the spec they wrote, not the pinned one.
    prior = {package_id(value, agent_dir): value for value in previous_packages}
    settings["packages"] = [prior.get(package_id(package, agent_dir), package) for package in settings.get("packages", [])
                            if package_id(package, agent_dir) not in owned]
    # The override replaced a release the user had declared before install; put that spec back like a retired package.
    displaced = [package for package in previous_packages if state.get("herdr_agents")
                 and package_id(package, agent_dir) == package_id(HERDR_AGENTS_SOURCE, agent_dir)]
    # A package the user had before install and the profile shed comes back with the rest.
    dropped_ids = {package_id(package, agent_dir) for package in profile_dropped(state)}
    displaced += [package for package in previous_packages if package_id(package, agent_dir) in dropped_ids]
    for package in [*state.get("retired_packages", []), *displaced]:
        if package not in settings["packages"]:
            if not ctx.skip_external:
                external.run(["pi", "install", package], home)
            settings["packages"].append(package)
    settings["packages"] = unique_packages(
        [package for package in previous_packages if package in settings["packages"]] + settings["packages"], agent_dir
    )
    if not settings["packages"]:
        settings.pop("packages")
    write_json(settings_path, settings)
