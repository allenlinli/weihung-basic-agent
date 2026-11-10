"""Saved host choices and portable, pinned Herdr plugin launchers."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import shutil

from pi_root import configs, external, packages
from pi_root.context import Context
from pi_root.jsonfile import read_json, write_json
from pi_root.paths import COMPUTER_USE_CONFIG
from pi_root.pins import CLAUDE_RULES_SOURCE, HERDR_PLUGINS
from support.pi import run_script

COMPUTER = "npm:@injaneity/pi-computer-use"


class FleetOptionsTests(unittest.TestCase):
    def test_saved_exclusion_migrates_existing_package_and_survives_bare_installs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            settings = home / ".pi/agent/settings.json"
            previous = {"source": COMPUTER + "@0.4.0", "extensions": ["*"]}
            write_json(settings, {"packages": [previous, "npm:user-package"], "userSetting": True})
            run_script("install.sh", home, "--skip-external", "--no-overlay",
                       "--computer-use", "disabled", "--pi-code-hooks", "enabled")
            for _ in range(2):
                run_script("install.sh", home, "--skip-external", "--no-overlay")
                values = read_json(settings)["packages"]
                self.assertFalse(any(packages.package_id(p, settings.parent) == COMPUTER for p in values))
                rules = [p for p in values if packages.package_id(p, settings.parent) == "npm:pi-code"]
                self.assertEqual(rules, [{"source": CLAUDE_RULES_SOURCE, "extensions": [
                    "extensions/claude-rules.ts", "extensions/hooks/index.ts"]}])
                self.assertTrue(read_json(settings)["userSetting"])
            self.assertFalse((home / COMPUTER_USE_CONFIG).exists())
            marker = read_json(home / ".pi/agent/.weihung-agent-root.json")
            self.assertEqual(marker["computer_use"], "disabled")
            self.assertEqual(marker["pi_code_hooks"], "enabled")
            run_script("uninstall.sh", home, "--skip-external")
            self.assertEqual(read_json(settings)["packages"], ["npm:user-package"])

    def test_explicit_reenable_and_hooks_disable_keep_profile_independent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            run_script("install.sh", home, "--skip-external", "--no-overlay",
                       "--computer-use", "disabled", "--pi-code-hooks", "enabled")
            run_script("install.sh", home, "--skip-external", "--no-overlay", "--profile", "default")
            self.assertNotIn(COMPUTER, {packages.package_id(p, home) for p in read_json(home / ".pi/agent/settings.json")["packages"]})
            run_script("install.sh", home, "--skip-external", "--no-overlay",
                       "--computer-use", "enabled", "--pi-code-hooks", "disabled")
            values = read_json(home / ".pi/agent/settings.json")["packages"]
            self.assertIn(COMPUTER, {packages.package_id(p, home) for p in values})
            self.assertEqual(next(p for p in values if packages.package_id(p, home) == "npm:pi-code")["extensions"],
                             ["extensions/claude-rules.ts"])
            run_script("uninstall.sh", home, "--skip-external")
            self.assertFalse((home / ".pi/agent/.weihung-agent-root.json").exists())

    def test_external_exclusion_removes_existing_declaration_without_installing_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            ctx = Context(home, {"computer_use": "disabled", "pi_code_hooks": "enabled"})
            write_json(ctx.settings_path, {"packages": [COMPUTER + "@0.4.0"]})
            packages.plan(ctx)
            calls = []
            def run(args: list[str], _home: Path) -> None:
                calls.append(args)
                if args[:2] == ["pi", "install"]:
                    settings = read_json(ctx.settings_path)
                    settings["packages"].append(args[2])
                    write_json(ctx.settings_path, settings)
            with mock.patch.object(external, "run", run), mock.patch.object(external, "require"), \
                    mock.patch.object(packages, "install_herdr_plugins"), \
                    mock.patch.object(packages.resources, "install_ported_resources"):
                packages.apply(ctx)
            self.assertIn(["pi", "remove", COMPUTER + "@0.4.0"], calls)
            self.assertFalse(any(args[:2] == ["pi", "install"] and args[2].startswith(COMPUTER) for args in calls))
            self.assertIn(["herdr", "integration", "install", "pi"], calls)

    def test_plugin_portability_covers_every_command_and_preserves_user_keys(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            config = home / ".config/herdr/config.toml"
            config.parent.mkdir(parents=True)
            original = '[[keys.command]]\nkey = "prefix+P"\ntype = "send_text"\ncommand = "personal"\n'
            config.write_text(original)
            calls = []
            def run(args: list[str], _home: Path) -> None:
                calls.append(args)
                if args[:2] == ["git", "clone"]:
                    checkout = Path(args[-1])
                    subdir = args[args.index("--branch") + 1]
                    # Both pinned plugin roots have argv manifests and a run.sh.
                    for _, repo, ref, _ in HERDR_PLUGINS:
                        if ref != subdir:
                            continue
                        root = checkout / repo.split("/")[-1]
                        root.mkdir(parents=True, exist_ok=True)
                        (root / "herdr-plugin.toml").write_text(
                            'id = "fixture"\n[[startup]]\ncommand = ["/bin/bash", "run.sh", "ok.mjs"]\n'
                            '[[events]]\non = "pane.exited"\ncommand = ["/bin/bash", "run.sh", "ok.mjs"]\n'
                            '[[actions]]\nid = "pin"\ncommand = ["/bin/bash", "run.sh", "ok.mjs"]\n')
                        (root / "run.sh").write_text('#!/usr/bin/env bash\nset -euo pipefail\nprintf "portable-ok\\n"\n')
            with mock.patch.object(external, "run", run), mock.patch.object(external, "capture", return_value=""), \
                    mock.patch.object(packages, "needs_portable_plugins", return_value=True):
                ctx = Context(home, {})
                packages.install_herdr_plugins(ctx)
                packages.install_herdr_plugins(ctx)
            self.assertEqual(len([c for c in calls if c[:2] == ["git", "clone"]]), len(HERDR_PLUGINS))
            linked = [Path(c[3]) for c in calls if c[:3] == ["herdr", "plugin", "link"]]
            self.assertTrue(linked)
            for root in linked:
                import tomllib
                manifest = tomllib.loads((root / "herdr-plugin.toml").read_text())
                for section in ("startup", "events", "actions"):
                    command = manifest[section][0]["command"]
                    self.assertNotEqual(command[0], "/bin/bash")
                    result = subprocess.run(command, cwd=root, env={**os.environ, "HOME": str(home)},
                                            text=True, capture_output=True, check=True)
                    self.assertEqual(result.stdout, "portable-ok\n")
            self.assertEqual(config.read_text(), original)

    def test_disabled_computer_use_retires_the_macos_helper(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            app = home / configs.COMPUTER_USE_APP
            (app / "Contents/MacOS").mkdir(parents=True)
            (app / "Contents/MacOS/bridge").write_text("")
            cache = home / configs.COMPUTER_USE_CACHE
            cache.mkdir(parents=True)
            for name in ("bridge.sock", "bridge.sock.lock"):
                (cache / name).write_text("")
            alive = {4242}
            commands = []
            def run(args, **_kwargs):
                commands.append(args)
                stdout = "".join(f"{pid}\n" for pid in alive) if args[0] == "pgrep" else ""
                return subprocess.CompletedProcess(args, 0 if stdout or args[0] != "pgrep" else 1, stdout, "")
            def kill(pid, sig):
                if pid not in alive:
                    raise ProcessLookupError(pid)
                if sig:
                    alive.discard(pid)
            ctx = Context(home, {"computer_use": "disabled"})
            with mock.patch.object(configs.sys, "platform", "darwin"), \
                    mock.patch.object(configs.subprocess, "run", run), mock.patch.object(configs.os, "kill", kill):
                configs.apply_computer_use(ctx)
                configs.apply_computer_use(ctx)
            self.assertFalse(app.exists())
            self.assertEqual(list(cache.iterdir()), [])
            self.assertEqual(commands.count(["tccutil", "reset", "All", configs.COMPUTER_USE_BUNDLE]), 1)
            import re
            pattern = commands[0][2]
            bridge = str(app / "Contents/MacOS/bridge")
            self.assertTrue(re.search(pattern, f"{bridge} serve --socket {cache}/bridge.sock"))
            self.assertFalse(re.search(pattern, f"/usr/bin/grep {bridge}"))

    def test_computer_use_helper_is_left_alone_when_enabled_or_off_macos(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            app = home / configs.COMPUTER_USE_APP
            app.mkdir(parents=True)
            with mock.patch.object(configs.subprocess, "run") as run:
                with mock.patch.object(configs.sys, "platform", "linux"):
                    configs.apply_computer_use(Context(home, {"computer_use": "disabled"}))
                with mock.patch.object(configs.sys, "platform", "darwin"):
                    configs.apply_computer_use(Context(home, {"computer_use": "enabled"}))
            run.assert_not_called()
            self.assertTrue(app.exists())

    def test_portable_bash_prefers_the_system_profile_over_store_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            stable = Path(directory) / "sw/bin/bash"
            stable.parent.mkdir(parents=True)
            stable.write_text("")
            with mock.patch.object(packages.shutil, "which", return_value="/nix/store/abc-bash-5/bin/bash"):
                with mock.patch.object(packages, "STABLE_BASH_PATHS", (str(stable),)):
                    self.assertEqual(packages.portable_bash(), str(stable))
                with mock.patch.object(packages, "STABLE_BASH_PATHS", ()):
                    with self.assertRaisesRegex(ValueError, "garbage"):
                        packages.portable_bash()

    def test_portable_root_rebuilds_a_stale_interpreter_and_prunes_old_refs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            plugin_id, repo, ref, _ = HERDR_PLUGINS[0]
            parent = home / ".local/share/weihung-agent-root/herdr-plugins" / plugin_id
            stale = parent / ref
            stale.mkdir(parents=True)
            (stale / "herdr-plugin.toml").write_text('[[startup]]\ncommand = ["/nix/store/gone/bin/bash", "run.sh"]\n')
            for leftover in ("v0.1.0", "tmpabc123"):
                (parent / leftover).mkdir()
            bash = shutil.which("bash")
            def run(args: list[str], _home: Path) -> None:
                if args[:2] == ["git", "clone"]:
                    root = Path(args[-1]) / repo.split("/", 2)[2]
                    root.mkdir(parents=True)
                    (root / "herdr-plugin.toml").write_text('[[startup]]\ncommand = ["/bin/bash", "run.sh"]\n')
            with mock.patch.object(external, "run", run), mock.patch.object(packages, "portable_bash", return_value=bash):
                root = packages.portable_plugin_root(home, plugin_id, repo, ref)
            self.assertEqual(root, stale)
            self.assertIn(json.dumps(bash), (root / "herdr-plugin.toml").read_text())
            self.assertEqual(sorted(path.name for path in parent.iterdir()), [ref])
            packages.remove_portable_plugin(home, plugin_id)
            self.assertFalse(parent.exists())

    def test_linked_plugin_match_uses_the_resolved_root(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "home"
            real_share = Path(directory) / "real-share"
            real_share.mkdir()
            (home / ".local").mkdir(parents=True)
            (home / ".local/share").symlink_to(real_share)
            roots = {}
            for plugin_id, repo, ref, _ in HERDR_PLUGINS:
                root = home / ".local/share/weihung-agent-root/herdr-plugins" / plugin_id / ref
                root.mkdir(parents=True)
                roots[plugin_id] = root
            listed = "\n".join(f"- {plugin_id} (X) enabled [local:{root.resolve()}]" for plugin_id, root in roots.items())
            calls = []
            with mock.patch.object(external, "run", lambda args, _home: calls.append(args)), \
                    mock.patch.object(external, "capture", return_value=listed), \
                    mock.patch.object(packages, "needs_portable_plugins", return_value=True), \
                    mock.patch.object(packages, "portable_plugin_root", lambda _h, plugin_id, _r, _f: roots[plugin_id]):
                packages.install_herdr_plugins(Context(home, {}))
            self.assertEqual(calls, [])

    def test_auto_update_leaves_the_hook_runtime_to_a_reviewed_bump(self) -> None:
        import importlib.util
        spec = importlib.util.spec_from_file_location("pi_pins", Path(__file__).resolve().parents[1] / "scripts/pi-pins.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertIn("pi-code", module.MANUAL_PINS)

    @unittest.skipUnless(os.environ.get("PI_TEST_REAL_PLUGINS"), "set PI_TEST_REAL_PLUGINS=1 for pinned GitHub/Herdr fixture checks")
    def test_real_pinned_portable_plugins_validate_without_starting_reaper(self) -> None:
        import tomllib
        import shutil
        self.assertIsNotNone(shutil.which("herdr"))
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            # No inherited Herdr socket/session: this isolated registry never touches a real server.
            env = {"PATH": os.environ["PATH"], "HOME": str(home), "XDG_CONFIG_HOME": str(home / ".config"),
                   "XDG_DATA_HOME": str(home / ".local/share"), "XDG_STATE_HOME": str(home / ".local/state")}
            for plugin_id, repo, ref, _ in HERDR_PLUGINS:
                root = packages.portable_plugin_root(home, plugin_id, repo, ref)
                manifest = tomllib.loads((root / "herdr-plugin.toml").read_text())
                commands = [entry["command"] for section in ("startup", "events", "actions")
                            for entry in manifest.get(section, []) if entry["command"][0] != "node"]
                self.assertTrue(commands)
                self.assertTrue(all(command[0] != "/bin/bash" for command in commands))
                # Drive the real upstream launcher harmlessly, never a reaper command.
                result = subprocess.run([commands[0][0], "run.sh", "--version"], cwd=root, env=env,
                                        text=True, capture_output=True, check=True)
                self.assertRegex(result.stdout, r"^v\d+\.")
                result = subprocess.run(["herdr", "plugin", "link", str(root), "--disabled"], env=env,
                                        text=True, capture_output=True, check=True)
                registered = json.loads(result.stdout)["result"]["plugin"]
                self.assertFalse(registered["enabled"])
                self.assertIn("net" if plugin_id.endswith("straw-boss") else "pin",
                              [action["id"] for action in registered["actions"]])
                # No action invocation: the disabled throwaway registry is removed with HOME.
                result = subprocess.run(["herdr", "plugin", "list"], env=env, text=True, capture_output=True, check=True)
                self.assertIn(plugin_id, result.stdout)
                with mock.patch.dict(os.environ, env, clear=True):
                    line = packages.plugin_line(home, plugin_id)
                self.assertIsNotNone(line, result.stdout)
                self.assertIn(str(root.resolve()), line, result.stdout)


if __name__ == "__main__":
    unittest.main()
