"""Exercise the pinned pi-code hooks through real pi RPC, without a model or credentials.

Run: PI_CODE_ROOT=/path/to/pi-code python3.14 tests/pi_hooks_protocol.py
The package is an explicit fixture: no install, real-home config, or network access.
"""

import json
import os
import selectors
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path


@unittest.skipUnless(os.environ.get("PI_CODE_ROOT"), "set PI_CODE_ROOT to the pinned pi-code fixture to run native RPC checks")
class NativeHooksTests(unittest.TestCase):
    def run_hook(self, decision: str, answer: bool, *, project_only: bool = False) -> tuple[bool, list[dict], list[dict]]:
        root = Path(os.environ["PI_CODE_ROOT"]).resolve()
        self.assertEqual(json.loads((root / "package.json").read_text())["version"], "1.7.0")
        pi = shutil.which("pi")
        self.assertIsNotNone(pi, "install pi before running the native protocol check")
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            cwd = home / "project"
            cwd.mkdir()
            agent = home / ".pi/agent"
            agent.mkdir(parents=True)
            script = home / "hook.py"
            log = home / "payloads.jsonl"
            output = home / "executed"
            script.write_text(
                "import json, sys\n"
                "from pathlib import Path\n"
                "payload = json.load(sys.stdin)\n"
                f"with Path({str(log)!r}).open('a') as stream: stream.write(json.dumps(payload) + '\\n')\n"
                f"print(json.dumps({{'hookSpecificOutput': {{'permissionDecision': {decision!r}, 'permissionDecisionReason': 'fixture decision'}}}}))\n"
            )
            config_home = cwd if project_only else home
            settings = config_home / ".claude/settings.json"
            settings.parent.mkdir(parents=True)
            settings.write_text(json.dumps({"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [
                {"type": "command", "command": sys.executable, "args": [str(script)]}
            ]}]}}))
            env = {"PATH": os.environ["PATH"], "HOME": str(home), "PI_CODING_AGENT_DIR": str(agent),
                   "CLAUDE_CONFIG_DIR": str(home / ".claude"), "PI_OFFLINE": "1", "PI_TELEMETRY": "0"}
            records = []
            with tempfile.TemporaryFile() as errors:
                process = subprocess.Popen([pi, "--mode", "rpc", "--no-session", "--offline", "--no-approve",
                                            "--no-extensions", "--no-skills", "--no-context-files", "--no-mcp",
                                            "-e", str(root / "extensions/hooks/index.ts")],
                                           cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errors)
                def send(value: dict) -> None:
                    process.stdin.write(json.dumps(value).encode() + b"\n")
                    process.stdin.flush()
                try:
                    send({"id": "ready", "type": "get_commands"})
                    pending = b""
                    deadline = time.monotonic() + 30
                    with selectors.DefaultSelector() as selector:
                        selector.register(process.stdout, selectors.EVENT_READ)
                        done = False
                        while not done and time.monotonic() < deadline:
                            if not selector.select(timeout=max(0, deadline - time.monotonic())):
                                break
                            data = os.read(process.stdout.fileno(), 65536)
                            if not data:
                                break
                            pending += data
                            while b"\n" in pending:
                                line, pending = pending.split(b"\n", 1)
                                record = json.loads(line)
                                records.append(record)
                                if record.get("type") == "extension_ui_request" and record.get("method") == "confirm":
                                    send({"type": "extension_ui_response", "id": record["id"], "confirmed": answer})
                                if record.get("type") == "response" and record.get("id") == "ready":
                                    self.assertTrue(record["success"], record)
                                    self.assertIn("hooks", [c["name"] for c in record["data"]["commands"]])
                                    send({"id": "bash", "type": "bash", "command": f"printf executed > {output}"})
                                if record.get("type") == "response" and record.get("id") == "bash":
                                    self.assertTrue(record["success"], record)
                                    done = True
                        self.assertTrue(done, records)
                finally:
                    process.stdin.close()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=10)
                    process.stdout.close()
                errors.seek(0)
                self.assertEqual(process.returncode, 0, errors.read().decode())
            payloads = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
            return output.exists(), records, payloads

    def test_ask_requires_confirmation_and_preserves_claude_payload(self) -> None:
        for answer in (False, True):
            with self.subTest(answer=answer):
                executed, records, payloads = self.run_hook("ask", answer)
                self.assertEqual(executed, answer, records)
                self.assertEqual(len([r for r in records if r.get("method") == "confirm"]), 1)
                self.assertEqual(payloads[0]["hook_event_name"], "PreToolUse")
                self.assertEqual(payloads[0]["tool_name"], "Bash")
                self.assertIn("command", payloads[0]["tool_input"])

    def test_deny_never_prompts_or_executes(self) -> None:
        executed, records, payloads = self.run_hook("deny", True)
        self.assertFalse(executed, records)
        self.assertTrue(payloads)
        self.assertFalse([r for r in records if r.get("method") == "confirm"])

    def test_untrusted_project_hooks_do_not_run(self) -> None:
        executed, records, payloads = self.run_hook("deny", False, project_only=True)
        self.assertTrue(executed, "the untrusted project's deny hook must not execute")
        self.assertEqual(payloads, [])
        self.assertFalse([r for r in records if r.get("method") == "confirm"])


if __name__ == "__main__":
    unittest.main()
