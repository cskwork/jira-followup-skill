"""Offline tests for jira-daily-wake (tick entry) and jira-worker (delegation).

Guard and paseo are stubs: no Jira scan, no agent launch."""

import os
import json
import signal
import time
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
WAKE = ROOT / "jira-daily-wake"
WORKER = ROOT / "jira-worker"


class StubFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.bin = self.dir / "bin"
        self.bin.mkdir()
        self.calls = self.dir / "calls.log"
        # paseo stub: records argv (one per line, NUL-free), prints a fake agent id
        self.write_exec(self.bin / "paseo", '#!/bin/bash\nfor a in "$@"; do printf "%s\\n" "$a"; done >> "$CALLS"\necho ---- >> "$CALLS"\n'
                        'printf "%s/%s" "${PASEO_AGENT_ID-unset}" "${PASEO_WORKSPACE_ID-unset}" > "$CALLS.env"\n'
                        'if [ "$1" = agent ] && [ "${2:-}" = ls ]; then echo "[]"; exit 0; fi\n'
                        'echo fake-agent-id\nexit "${PASEO_RC:-0}"\n')
        self.env = dict(os.environ, HOME=str(self.dir), JIRA_WAKE_FOREGROUND="1", PATH=f"{self.bin}:{os.environ['PATH']}", CALLS=str(self.calls),
                        JIRA_WAKE_CWD=str(self.dir), JIRA_WORKER_CWD=str(self.dir))

        state = self.dir / ".jira-followup"
        (state / "runs/finished").mkdir(parents=True)
        (state / "runs/finished/ended").touch()

    def tearDown(self):
        self.tmp.cleanup()

    def write_exec(self, path, body):
        path.write_text(body)
        path.chmod(0o755)

    def paseo_calls(self):
        if not self.calls.exists():
            return []
        return [c.strip("\n").split("\n") for c in self.calls.read_text().split("----\n") if c.strip()]


class WakeChecks(StubFixture):
    def guard(self, output):
        guard = self.dir / "guard"
        released = self.dir / "released"
        self.write_exec(guard, f'#!/bin/bash\nif [ "${{1:-}}" = --release ]; then touch "{released}"; exit 0; fi\n'
                               f'printf "%b\\n" "{output}"\n')
        self.env["JIRA_WAKE_GUARD"] = str(guard)
        return released

    def wake(self, **extra):
        env = dict(self.env, **extra)
        return subprocess.run(["/bin/bash", str(WAKE)], env=env, capture_output=True, text=True, timeout=20)

    def test_idle_or_duplicate_launches_nothing(self):
        released = self.guard("jira-daily-guard: skipping duplicate (idle: events=0, no runnable work)")
        response = self.wake()
        self.assertEqual(response.returncode, 0)
        self.assertEqual(self.paseo_calls(), [])
        self.assertFalse(released.exists())

    def test_tick_launches_detached_opus_high_orchestrator(self):
        self.env["PASEO_RC"] = "0"
        released = self.guard("run\\nmode=tick")
        response = self.wake()
        self.assertEqual(response.returncode, 0, response.stdout + response.stderr)
        calls = self.paseo_calls()
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0], ["agent", "ls", "--json", "--label", "run=finished"])
        argv = calls[1]
        self.assertEqual(argv[0], "run")
        self.assertIn("--no-wait", argv)
        for flag, value in (("--provider", "claude"), ("--model", "claude-opus-5-5"),
                            ("--thinking", "high"), ("--mode", "bypassPermissions")):
            self.assertEqual(argv[argv.index(flag) + 1], value)
        self.assertIn("mode=tick", argv[-1])
        self.assertIn("ORCHESTRATOR.md", argv[-1])
        self.assertFalse(released.exists())  # orchestrator owns the lock now

    def test_orchestrator_is_not_a_child_of_the_schedule_agent(self):
        # Paseo archives a finished schedule run agent and cascades the archive to
        # children in its workspace; the orchestrator must not inherit that parent.
        self.guard("run\\nmode=tick")
        response = self.wake(PASEO_AGENT_ID="schedule-run-agent", PASEO_WORKSPACE_ID="wks_schedule")
        self.assertEqual(response.returncode, 0, response.stdout + response.stderr)
        self.assertEqual((self.dir / "calls.log.env").read_text(), "unset/unset")

    def test_manual_now_with_key_passes_manual_and_focus(self):
        guard = self.dir / "guard"
        seen = self.dir / "guard-args"
        self.write_exec(guard, f'#!/bin/bash\nprintf "%s\\n" "$@" > "{seen}"\nprintf "manual run\\nmode=tick\\n"\n')
        self.env["JIRA_WAKE_GUARD"] = str(guard)
        response = subprocess.run(["/bin/bash", str(WAKE), "--now", "--key", "PROJ-1389"], env=self.env,
                                  capture_output=True, text=True, timeout=20)
        self.assertEqual(response.returncode, 0, response.stdout + response.stderr)
        self.assertEqual(seen.read_text().split(), ["--manual"])
        calls = self.paseo_calls()
        self.assertEqual([call[:2] for call in calls], [["agent", "ls"], ["run", "--no-wait"]])
        prompt = calls[1][-1]
        self.assertIn("trigger=manual", prompt)
        self.assertIn("focus=PROJ-1389", prompt)

    def test_key_without_now_or_bad_key_is_usage_error(self):
        self.guard("run\\nmode=tick")
        for args in (["--key", "PROJ-1389"], ["--now", "--key", "1389"], ["--bogus"]):
            response = subprocess.run(["/bin/bash", str(WAKE), *args], env=self.env,
                                      capture_output=True, text=True, timeout=20)
            self.assertEqual(response.returncode, 2, args)
        self.assertEqual(self.paseo_calls(), [])

    def test_launch_failure_releases_lock(self):
        self.env["PASEO_RC"] = "1"
        released = self.guard("run\\nmode=morning")
        response = self.wake()
        self.assertEqual(response.returncode, 1)
        self.assertTrue(released.exists())
        self.assertIn("launch failed", response.stdout)

    def test_missing_mode_releases_lock(self):
        released = self.guard("run")
        response = self.wake()
        self.assertEqual(response.returncode, 1)
        self.assertTrue(released.exists())
        self.assertEqual(self.paseo_calls(), [])

    def test_dry_run_prints_command_and_releases(self):
        released = self.guard("run\\nmode=tick")
        response = self.wake(JIRA_WAKE_DRY_RUN="1")
        self.assertEqual(response.returncode, 0)
        self.assertIn("dry-run:", response.stdout)
        self.assertEqual(self.paseo_calls(), [])
        self.assertTrue(released.exists())


class DetachedWakeChecks(StubFixture):
    guard = WakeChecks.guard
    wake = WakeChecks.wake
    # Keep the original contract cases in WakeChecks; this class adds tick lifecycle cases.
    def test_tick_returns_quickly_and_survives_parent_process_group_kill(self):
        self.guard("mode=tick")
        guard = Path(self.env["JIRA_WAKE_GUARD"])
        guard.write_text(guard.read_text().replace('printf "%b', 'sleep 6\nprintf "%b'))
        env = dict(self.env, JIRA_WAKE_FOREGROUND="0")
        started = time.monotonic()
        parent = subprocess.Popen(["/bin/bash", "-c", '/bin/bash "$1"; sleep 30', "_", str(WAKE)],
                                  env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  text=True, start_new_session=True)
        try:
            line = parent.stdout.readline().strip()
            self.assertLess(time.monotonic() - started, 5)
            self.assertRegex(line, r"^wake=detached pid=\d+ log=")
            os.killpg(parent.pid, signal.SIGTERM)
            parent.wait(timeout=5)
            ledger = self.dir / ".jira-followup/ticks.log"
            deadline = time.monotonic() + 12
            while time.monotonic() < deadline:
                events = [json.loads(l) for l in ledger.read_text().splitlines()] if ledger.exists() else []
                if any(e["event"] == "end" for e in events):
                    break
                time.sleep(0.05)
            self.assertEqual([e["event"] for e in events], ["start", "end"])
            self.assertEqual(events[-1]["outcome"], "exit=0")
            self.assertEqual(events[0]["pid"], int(line.split("pid=")[1].split()[0]))
            self.assertEqual([call[:2] for call in self.paseo_calls()],
                             [["agent", "ls"], ["run", "--no-wait"]])
            log = Path(line.split(" log=", 1)[1])
            self.assertIn("orchestrator=fake-agent-id", log.read_text())
        finally:
            if parent.poll() is None:
                os.killpg(parent.pid, signal.SIGKILL)
                parent.wait(timeout=5)
            parent.stdout.close()
            parent.stderr.close()

    def test_lost_slots_are_reported_once_and_recent_attempt_is_not_lost(self):
        self.guard("skipping duplicate")
        state = self.dir / ".jira-followup"
        state.mkdir(parents=True, exist_ok=True)
        ledger = state / "ticks.log"
        def event(slot, action, at, identity):
            return dict(slot=f"2026-10-22T{slot}:00+09:00", event=action,
                        at=f"2026-10-22T{at}:00+09:00", id=identity, pid=123)
        events = [event("08:00", "start", "08:00", "first"),
                  event("08:00", "end", "08:05", "first"),
                  event("09:00", "start", "09:00", "stale"),
                  event("09:30", "start", "09:30", "recent")]
        ledger.write_text("".join(json.dumps(e) + "\n" for e in events))
        for _ in range(2):
            response = self.wake(JIRA_WAKE_AT="2026-10-22T10:00:00+09:00")
            self.assertEqual(response.returncode, 0, response.stderr)
        lines = (state / "followup-2026-10-22.md").read_text().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertIn("lost-tick: 2026-10-22T08:30:00+09:00 no-start", lines[0])
        self.assertIn("lost-tick: 2026-10-22T09:00:00+09:00 start-without-end", lines[1])
        for line in lines:
            self.assertEqual(len(line.split(" | ")), 8)
            self.assertTrue(line.endswith(" | none | ticks.log"))

    def test_first_tick_without_ledger_reports_no_lost_slots(self):
        # Slots before the ledger existed are unknown, not lost (rollout day must not flood the journal).
        self.guard("skipping duplicate")
        self.assertEqual(self.wake(JIRA_WAKE_AT="2026-10-22T14:30:00+09:00").returncode, 0)
        self.assertFalse((self.dir / ".jira-followup/followup-2026-10-22.md").exists())

    def test_partial_ledger_record_does_not_hide_next_tick(self):
        self.guard("skipping duplicate")
        state = self.dir / ".jira-followup"
        state.mkdir(parents=True, exist_ok=True)
        ledger = state / "ticks.log"
        ledger.write_text('[]\n{"event":"start"')
        self.assertEqual(self.wake(JIRA_WAKE_AT="2026-10-22T08:00:00+09:00").returncode, 0)
        lines = ledger.read_text().splitlines()
        self.assertEqual(json.loads(lines[-2])["event"], "start")
        self.assertEqual(json.loads(lines[-1])["event"], "end")

    def test_weekends_do_not_report_lost_slots(self):
        self.guard("skipping duplicate")
        self.assertEqual(self.wake(JIRA_WAKE_AT="2026-10-24T10:00:00+09:00").returncode, 0)
        self.assertFalse((self.dir / ".jira-followup/followup-2026-10-24.md").exists())

    def test_manual_waits_for_guard_and_does_not_create_tick_ledger(self):
        self.guard("mode=tick")
        guard = Path(self.env["JIRA_WAKE_GUARD"])
        guard.write_text(guard.read_text().replace('printf "%b', 'sleep 0.2\nprintf "%b'))
        started = time.monotonic()
        response = subprocess.run(["/bin/bash", str(WAKE), "--now", "--key", "PROJ-1389"],
                                  env=dict(self.env, JIRA_WAKE_FOREGROUND="0"), capture_output=True,
                                  text=True, timeout=10)
        self.assertGreaterEqual(time.monotonic() - started, 0.2)
        self.assertEqual(response.returncode, 0, response.stderr)
        self.assertIn("orchestrator=fake-agent-id", response.stdout)
        self.assertNotIn("wake=detached", response.stdout)
        self.assertFalse((self.dir / ".jira-followup/ticks.log").exists())


class WorkerChecks(StubFixture):
    def worker(self, *args):
        state = self.dir / ".jira-followup"
        (state / ".running-fixture").write_text(json.dumps({"token": "fixture"}))
        os.replace(state / ".running-fixture", state / "running.json")
        return subprocess.run(["/bin/bash", str(WORKER), *args, "--token", "fixture"], env=self.env,
                              capture_output=True, text=True, timeout=20)

    def brief(self):
        path = self.dir / "brief.md"
        path.write_text("목표: 시험\n")
        return str(path)

    def test_roles_map_to_rules_md_models(self):
        expected = {"sol": ("codex", "gpt-6.1-sol", "high", "full-access"),
                    "opus": ("claude", "claude-opus-5-5", "medium", "bypassPermissions")}
        for role, (provider, model, thinking, mode) in expected.items():
            self.calls.unlink(missing_ok=True)
            response = self.worker(role, "PROJ-1 unit", self.brief())
            self.assertEqual(response.returncode, 0, response.stderr)
            argv = self.paseo_calls()[0]
            self.assertNotIn("--no-wait", argv)
            for flag, value in (("--provider", provider), ("--model", model),
                                ("--thinking", thinking), ("--mode", mode), ("--wait-timeout", "90m")):
                self.assertEqual(argv[argv.index(flag) + 1], value)
            self.assertTrue(argv[argv.index("--output-schema") + 1].endswith("worker-result.schema.json"))
            self.assertIn("목표: 시험", argv[-1])

    def test_bad_usage_exits_2_without_launch(self):
        self.assertEqual(self.worker("haiku", "x", self.brief()).returncode, 2)
        self.assertEqual(self.worker("sol", "x", str(self.dir / "missing.md")).returncode, 2)
        self.assertEqual(self.paseo_calls(), [])

    def test_worker_failure_propagates(self):
        self.env["PASEO_RC"] = "3"
        self.assertEqual(self.worker("sol", "x", self.brief()).returncode, 3)


if __name__ == "__main__":
    unittest.main()
