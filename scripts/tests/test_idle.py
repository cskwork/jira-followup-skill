"""Offline idle-gate and guard contracts; all child commands are local stubs."""

import datetime
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
IDLE = ROOT / "followup_idle.py"
GUARD = ROOT / "jira-daily-guard"
IDLE_MESSAGE = "jira-daily-guard: skipping duplicate (idle: events=0, no runnable work)"

STUB = '''import argparse
import json
from pathlib import Path
import sys

parser = argparse.ArgumentParser()
parser.add_argument("kind")
parser.add_argument("config")
parser.add_argument("calls")
parser.add_argument("message", nargs="?")
parser.add_argument("--out")
parser.add_argument("--need")
parser.add_argument("--fail-fast", action="store_true")
args = parser.parse_args()
config = json.loads(Path(args.config).read_text())
with Path(args.calls).open("a") as output:
    output.write(json.dumps({"kind": args.kind, "out": args.out, "need": args.need}) + "\\n")
if args.kind == "scan" and args.out and not config.get("skip_write"):
    payload = config.get("scan", {"events": [], "errors": []})
    Path(args.out).write_text(payload if isinstance(payload, str) else json.dumps(payload))
print(config.get(args.kind + "_stdout", "fixture complete"))
print(config.get(args.kind + "_stderr", ""), file=sys.stderr, end="")
sys.exit(config.get(args.kind + "_code", 0))
'''


class IdleFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="followup idle ")
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.state = self.home / ".jira-followup"
        self.state.mkdir(parents=True)
        self.stub = self.home / "command stub.py"
        self.stub.write_text(STUB)
        self.config = self.home / "commands.json"
        self.calls = self.home / "calls.jsonl"
        self.settings = {}
        self.configure()
        self.env = {key: value for key, value in os.environ.items()
                    if key not in ("FOLLOWUP_IDLE_SCAN_CMD", "FOLLOWUP_IDLE_PREFLIGHT_CMD")}
        self.env.update(HOME=str(self.home),
                        FOLLOWUP_IDLE_SCAN_CMD=self.command("scan"),
                        FOLLOWUP_IDLE_PREFLIGHT_CMD=self.command("preflight"),
                        FOLLOWUP_IDLE_NOTIFY_CMD=self.command("notify"))

        binary = self.home / "bin"
        binary.mkdir()
        for name, body in (("paseo", "#!/bin/sh\necho '[]'\n"), ("osascript", "#!/bin/sh\nexit 0\n")):
            path = binary / name
            path.write_text(body)
            path.chmod(0o755)
        self.env["PATH"] = f"{binary}:{os.environ['PATH']}"

    def configure(self, **settings):
        self.settings.update(settings)
        self.config.write_text(json.dumps(self.settings))

    def command(self, kind):
        return shlex.join([sys.executable, str(self.stub), kind,
                           str(self.config), str(self.calls)])

    def write_json(self, name, data):
        path = self.state / name
        path.write_text(json.dumps(data))
        return path

    def queue(self, waiting="reporter", **extra):
        return self.write_json("followup-queue.json", {"PROJ-1001": {"waiting": waiting, **extra}})

    def recorded(self, kind=None):
        records = [json.loads(line) for line in self.calls.read_text().splitlines()] if self.calls.exists() else []
        return [record for record in records if kind is None or record["kind"] == kind]

    def check(self, *, explicit=False):
        args = [sys.executable, str(IDLE), "--dir", str(self.state)]
        if explicit:
            args.extend(["--scan-cmd", self.command("scan"),
                         "--preflight-cmd", self.command("preflight")])
        response = subprocess.run(args, env=self.env, capture_output=True, text=True, timeout=10)
        self.assertEqual(response.stderr, "")
        self.assertEqual(len(response.stdout.splitlines()), 1)
        data = json.loads(response.stdout)
        self.assertIsInstance(data["idle"], bool)
        self.assertIsInstance(data["reasons"], list)
        self.assertTrue(all(isinstance(reason, str) for reason in data["reasons"]))
        self.assertIsInstance(data["events"], int)
        self.assertIsInstance(data["queue"], dict)
        return response.returncode, data

    def assert_idle(self, expected, *, code=None, explicit=False):
        actual_code, data = self.check(explicit=explicit)
        self.assertEqual(actual_code, (0 if expected else 1) if code is None else code)
        self.assertEqual(data["idle"], expected)
        return data


class IdleChecks(IdleFixture):
    def test_missing_optional_state_is_idle_and_scan_receives_output_path(self):
        data = self.assert_idle(True, explicit=True)
        self.assertEqual((data["events"], data["queue"]), (0, {}))
        self.assertEqual(self.recorded("scan")[0]["out"], str(self.state / "followup-scan.json"))
        self.assertEqual(self.recorded("preflight"), [])

    def test_all_waiting_with_absent_or_used_approval_is_idle(self):
        self.write_json("followup-queue.json", {
            "PROJ-1001": {"waiting": "reporter"},
            "PROJ-1002": {"waiting": "human-risk"},
            "PROJ-1003": {"waiting": "stg-approval"},
        })
        self.assert_idle(True)
        self.write_json("approvals.json", {"PROJ-1003": {"stg": "fixture approval", "usedAt": "2026-10-20"}})
        data = self.assert_idle(True)
        self.assertEqual(data["queue"], {"PROJ-1001": "reporter", "PROJ-1002": "human-risk", "PROJ-1003": "stg-approval"})

    def test_new_event_resumes_work(self):
        self.configure(scan={"events": [{"key": "PROJ-1001", "id": "100"}], "errors": []})
        data = self.assert_idle(False)
        self.assertEqual(data["events"], 1)

    def test_null_or_missing_waiting_resumes_work(self):
        for row in ({"waiting": None}, {}):
            with self.subTest(row=row):
                self.write_json("followup-queue.json", {"PROJ-1001": row})
                self.assert_idle(False)

    def test_unused_stg_approval_resumes_work(self):
        self.queue("stg-approval")
        self.write_json("approvals.json", {"PROJ-1001": {"stg": "fixture approval", "at": "2026-10-20"}})
        self.assert_idle(False)
        self.queue("reporter")
        self.assert_idle(False)

    def test_unused_approval_without_matching_queue_item_resumes_work(self):
        self.queue("stg-approval")
        self.write_json("approvals.json", {"PROJ-9999": {"stg": "fixture approval"}})
        self.assert_idle(False)

    def test_approval_without_stg_authorization_does_not_resume(self):
        self.queue("stg-approval")
        self.write_json("approvals.json", {"PROJ-1001": {"at": "2026-10-20"}})
        self.assert_idle(True)

    def test_auth_failure_in_any_scan_channel_resumes_and_is_sanitized(self):
        for channel in ("stdout", "stderr", "errors"):
            with self.subTest(channel=channel):
                self.settings = {}
                text = "401 Unauthorized fixture-sensitive-value"
                if channel == "errors":
                    self.configure(scan={"events": [], "errors": [text]})
                else:
                    self.configure(**{"scan_" + channel: text})
                data = self.assert_idle(False)
                self.assertNotIn("fixture-sensitive-value", json.dumps(data))

    def test_nonzero_scan_and_reported_errors_resume(self):
        self.configure(scan_code=1)
        self.assert_idle(False)
        self.configure(scan_code=0, scan={"events": [], "errors": ["fixture scan failed"]})
        self.assert_idle(False)

    def test_environment_wait_probes_needs_and_resumes_on_pass(self):
        for waiting in ("env-human", "env-down"):
            for preflight_code in (0, 10, 20):
                with self.subTest(waiting=waiting, preflight_code=preflight_code):
                    self.queue(waiting, needs="dev-db,dev-ai")
                    self.configure(preflight_code=preflight_code)
                    self.assert_idle(preflight_code != 0)
                    self.assertEqual(self.recorded("preflight")[-1]["need"], "dev-db,dev-ai")

    def test_environment_wait_without_needs_does_not_probe(self):
        self.queue("env-human")
        self.assert_idle(True)
        self.assertEqual(self.recorded("preflight"), [])

    def test_malformed_state_and_scan_are_internal_errors(self):
        for filename in ("followup-queue.json", "approvals.json"):
            for payload in ("{", "[]"):
                with self.subTest(filename=filename, payload=payload):
                    path = self.state / filename
                    path.write_text(payload)
                    self.assert_idle(False, code=2)
                    path.unlink()
        for payload in ("{", [], {"events": [], "errors": "invalid"}):
            with self.subTest(scan=payload):
                self.configure(scan=payload)
                self.assert_idle(False, code=2)

    def test_scan_that_writes_nothing_cannot_reuse_stale_idle_result(self):
        self.write_json("followup-scan.json", {"events": [], "errors": []})
        self.configure(skip_write=True)
        self.assert_idle(False, code=2)

    def test_malformed_queue_and_approval_rows_are_internal_errors(self):
        for filename, row in (("followup-queue.json", []),
                              ("followup-queue.json", {"waiting": 1}),
                              ("followup-queue.json", {"waiting": "env-human", "needs": ["dev-db"]}),
                              ("approvals.json", [])):
            with self.subTest(filename=filename, row=row):
                path = self.write_json(filename, {"PROJ-1001": row})
                self.assert_idle(False, code=2)
                path.unlink()

    def test_explicit_command_options_override_environment(self):
        self.env["FOLLOWUP_IDLE_SCAN_CMD"] = "/nonexistent/fixture-scan"
        self.env["FOLLOWUP_IDLE_PREFLIGHT_CMD"] = "/nonexistent/fixture-preflight"
        self.assert_idle(True, explicit=True)


class GuardChecks(IdleFixture):
    def setUp(self):
        super().setUp()
        binary = self.home / ".local" / "bin" / "paseo-once"
        binary.parent.mkdir(parents=True)
        binary.write_text('#!/bin/bash\necho "${FIXTURE_PASEO_OUTPUT:-run}"\n')
        binary.chmod(0o755)
        self.today = datetime.date.today().isoformat()
        self.log = self.state / ("followup-" + self.today + ".md")

    def tick(self):
        (self.state / "done-today").write_text(self.today + "\n")

    def guard(self, script=GUARD):
        response = subprocess.run(["/bin/bash", str(script)], env=self.env,
                                  capture_output=True, text=True, timeout=10)
        self.assertEqual(response.returncode, 0)
        self.assertEqual(response.stderr, "")
        return response.stdout

    def test_tick_idle_skips_without_lock_and_appends_one_table_line(self):
        self.tick()
        before = "# Previous evidence\n\n| earlier | preserved |\n"
        self.log.write_text(before)
        output = self.guard()
        self.assertEqual(output.splitlines(), ["run", IDLE_MESSAGE])
        self.assertNotIn("mode=tick", output)
        self.assertFalse((self.state / "running").exists())
        after = self.log.read_text()
        self.assertTrue(after.startswith(before))
        appended = after[len(before):].splitlines()
        self.assertEqual(len(appended), 1)
        columns = appended[0].split(" | ")
        self.assertEqual(len(columns), 8)
        self.assertEqual(datetime.datetime.fromisoformat(columns[0]).date().isoformat(), self.today)
        self.assertEqual(columns[1:5], ["—", "—", "스캔", "—"])
        self.assertTrue(columns[5].startswith("idle-skip(guard):"))
        self.assertEqual(columns[6:], ["none", "followup-scan.json"])
        self.assertEqual(len(self.recorded("scan")), 1)

    def test_manual_skips_dedupe_and_idle_but_respects_lock(self):
        self.tick()
        self.env["FIXTURE_PASEO_OUTPUT"] = "paseo-once: skipping duplicate"
        response = subprocess.run(["/bin/bash", str(GUARD), "--manual"], env=self.env,
                                  capture_output=True, text=True, timeout=10)
        self.assertEqual(response.returncode, 0)
        self.assertEqual(response.stdout.splitlines(), ["manual run", "mode=tick"])
        self.assertTrue((self.state / "running").exists())
        self.assertEqual(self.recorded("scan"), [])
        again = subprocess.run(["/bin/bash", str(GUARD), "--manual"], env=self.env,
                               capture_output=True, text=True, timeout=10)
        self.assertIn("skipping duplicate (running)", again.stdout)

    def test_release_removes_lock_without_scanning(self):
        lock = self.state / "running"
        lock.write_text("1\n")
        (self.state / "liveness").write_text("off\n")
        response = subprocess.run(["/bin/bash", str(GUARD), "--release"], env=self.env,
                                  capture_output=True, text=True, timeout=10)
        self.assertEqual(response.returncode, 0)
        self.assertEqual(response.stdout.splitlines(), ["jira-daily-guard: released"])
        self.assertFalse(lock.exists())
        self.assertEqual(self.recorded("scan"), [])

    def test_tick_actionable_work_sets_mode_and_lock(self):
        self.tick()
        self.queue(None)
        output = self.guard()
        self.assertIn("mode=tick", output)
        self.assertNotIn(IDLE_MESSAGE, output)
        self.assertTrue((self.state / "running").read_text().strip().isdigit())

    def test_morning_does_not_scan_and_sets_lock(self):
        output = self.guard()
        self.assertIn("mode=morning", output)
        self.assertTrue((self.state / "running").exists())
        self.assertEqual(self.recorded(), [])

    def test_scan_internal_error_fails_open_into_tick(self):
        self.tick()
        self.configure(scan="{")
        output = self.guard()
        self.assertIn("mode=tick", output)
        self.assertTrue((self.state / "running").exists())

    def test_existing_slot_duplicate_exits_before_scan_or_lock(self):
        self.tick()
        self.env["FIXTURE_PASEO_OUTPUT"] = "paseo-once: skipping duplicate"
        output = self.guard()
        self.assertIn("skipping duplicate", output)
        self.assertNotIn("mode=", output)
        self.assertFalse((self.state / "running").exists())
        self.assertEqual(self.recorded(), [])

    def test_recent_running_lock_is_preserved_without_scan(self):
        self.tick()
        running = self.state / "running"
        marker = str(int(time.time())) + "\n"
        running.write_text(marker)
        output = self.guard()
        self.assertIn("skipping duplicate (running)", output)
        self.assertEqual(running.read_text(), marker)
        self.assertEqual(self.recorded(), [])

    def test_stale_running_lock_allows_tick(self):
        self.tick()
        self.queue(None)
        running = self.state / "running"
        old = str(int(time.time()) - 10860)
        running.write_text(old + "\n")
        self.assertIn("mode=tick", self.guard())
        self.assertNotEqual(running.read_text().strip(), old)

    def test_guard_symlink_finds_sibling_idle_script(self):
        self.tick()
        link = self.home / "guard link"
        link.symlink_to(GUARD)
        self.assertIn(IDLE_MESSAGE, self.guard(script=link))
        self.assertEqual(len(self.recorded("scan")), 1)


if __name__ == "__main__":
    unittest.main()
