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

from pi_root import external, packages
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
