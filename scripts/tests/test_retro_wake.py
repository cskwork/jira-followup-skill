"""Offline regression checks for completed orchestrator cleanup and guard evidence."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
WAKE = ROOT / "jira-daily-wake"
GUARD = ROOT / "jira-daily-guard"


class RetroWakeChecks(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.state = self.home / "isolated state"
        self.state.mkdir()
        (self.state / "runs/finished").mkdir(parents=True)
        (self.state / "runs/finished/ended").touch()
        binary = self.home / "bin"
        binary.mkdir()
        self.calls = self.home / "calls.jsonl"
        paseo = binary / "paseo"
        paseo.write_text('''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
with open(os.environ["CALLS"], "a") as stream:
    stream.write(json.dumps(args) + "\\n")
if args[:2] == ["agent", "ls"]:
    print(os.environ.get("AGENTS", "[]"))
    sys.exit(int(os.environ.get("LIST_RC", "0")))
if args[:2] == ["agent", "archive"]:
    sys.exit(1 if args[2] == os.environ.get("FAIL_ARCHIVE") else 0)
if args[0] == "run":
    print("new-orchestrator")
''')
        paseo.chmod(0o755)
        guard = self.home / "guard"
        guard.write_text('''#!/bin/bash
printf '%s\\n' "$@" > "$GUARD_ARGS"
case " $* " in *" --release "*) exit 0 ;; esac
printf '%s\\n' "${GUARD_OUTPUT:-mode=tick}"
''')
        guard.chmod(0o755)
        self.env = dict(os.environ, HOME=str(self.home), PATH=f"{binary}:{os.environ['PATH']}",
                        CALLS=str(self.calls), GUARD_ARGS=str(self.home / "guard-args"),
                        JIRA_WAKE_GUARD=str(guard), JIRA_WAKE_CWD=str(self.home),
                        JIRA_WAKE_DIR=str(self.state), JIRA_WAKE_FOREGROUND="1")

    def wake(self, *args, **env):
        return subprocess.run(["/bin/bash", str(WAKE), *args], env=dict(self.env, **env),
                              text=True, capture_output=True, timeout=15)

    def recorded(self):
        return [json.loads(line) for line in self.calls.read_text().splitlines()] if self.calls.exists() else []

    def test_archives_only_ended_token_idle_and_closed_agents_before_launch(self):
        agents = [{"id": "old-idle", "status": "idle"}, {"id": "active", "status": "running"},
                  {"id": "old-done", "status": "closed"}]
        result = self.wake("--now", AGENTS=json.dumps(agents))
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.recorded()
        self.assertEqual(calls[0], ["agent", "ls", "--json", "--label", "run=finished"])
        self.assertEqual(calls[1], ["agent", "archive", "old-idle"])
        self.assertEqual(calls[2], ["agent", "archive", "old-done"])
        self.assertEqual(calls[3][0], "run")
        self.assertIn("archived=2 archive_failed=0", result.stdout)

    def test_archive_failure_continues_other_archives_and_launch(self):
        agents = [{"id": "broken", "status": "idle"}, {"id": "finished", "status": "idle"}]
        result = self.wake("--now", AGENTS=json.dumps(agents), FAIL_ARCHIVE="broken")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([call[:2] for call in self.recorded()],
                         [["agent", "ls"], ["agent", "archive"], ["agent", "archive"],
                          ["run", "--no-wait"]])
        self.assertIn("archived=1 archive_failed=1", result.stdout)

    def test_list_failure_or_invalid_json_still_launches(self):
        for env in ({"LIST_RC": "1"}, {"AGENTS": "not-json"}):
            with self.subTest(env=env):
                self.calls.unlink(missing_ok=True)
                result = self.wake("--now", **env)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.recorded()[-1][0], "run")
                self.assertIn("archived=0 archive_failed=1", result.stdout)

    def test_prompt_carries_guard_reasons_from_selected_state(self):
        (self.state / "guard-last.json").write_text(json.dumps({"reasons": [
            "queue-runnable:PROJ-1389", "events:1"]}))
        result = self.wake("--now")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("guard_reasons=queue-runnable:PROJ-1389,events:1", self.recorded()[-1][-1])

    def test_missing_or_invalid_guard_evidence_does_not_block_launch(self):
        (self.state / "guard-last.json").write_text("{")
        result = self.wake("--now")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("guard_reasons=", self.recorded()[-1][-1])

    def test_duplicate_and_dry_run_do_not_archive(self):
        result = self.wake("--now", GUARD_OUTPUT="skipping duplicate")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.recorded(), [])
        result = self.wake("--now", JIRA_WAKE_DRY_RUN="1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.recorded(), [])

    def test_dir_argument_reaches_guard_and_tick_supervisor(self):
        alternate = self.home / "alternate state"
        alternate.mkdir()
        (alternate / "guard-last.json").write_text(json.dumps({"reasons": ["queue-runnable:PROJ-2"]}))
        result = self.wake("--dir", str(alternate))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.home / "guard-args").read_text().splitlines(), ["--dir", str(alternate)])
        self.assertTrue((alternate / "ticks.log").exists())
        self.assertFalse((self.home / ".jira-followup/ticks.log").exists())
        self.assertIn("guard_reasons=queue-runnable:PROJ-2", self.recorded()[-1][-1])
        self.assertIn("state_dir=" + json.dumps(str(alternate.resolve())), self.recorded()[-1][-1])

    def test_guard_dir_argument_scopes_manual_lock_and_release(self):
        for args in (["--manual", "--dir", str(self.state)], ["--dir", str(self.state), "--release", "--token", "TOKEN"]):
            if "TOKEN" in args:
                args[-1] = json.loads((self.state / "running.json").read_text())["token"]
            result = subprocess.run(["/bin/bash", str(GUARD), *args], env=self.env,
                                    text=True, capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            if "--manual" in args:
                self.assertIn("mode=morning", result.stdout)
                self.assertTrue((self.state / "running").exists())
            else:
                self.assertIn("released", result.stdout)
                self.assertFalse((self.state / "running").exists())
        self.assertFalse((self.home / ".jira-followup/running").exists())


if __name__ == "__main__":
    unittest.main()
