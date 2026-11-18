"""임시 디렉터리와 가짜 프로세스 신원만 쓰는 결정 수신기 검증."""

from datetime import datetime, timedelta, timezone
import json
import fcntl
import subprocess
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import decision_page as dp
import decision_replies as replies
from state_update import atomic_json

NOW = datetime(2026, 10, 30, 2, tzinfo=timezone.utc)


class Replies(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.pages = self.directory / "decision-pages"
        self.pages.mkdir()
        self.registry = {"D1": {"name": "choice", "kind": "choice", "key": "PROJ-1000",
                              "optionIds": ["wait", "skip"], "state": "open",
                              "url": "http://127.0.0.1:12345/?t=fake",
                              "createdAt": (NOW - timedelta(minutes=2)).isoformat()}}
        self.write_registry()
        process_start = patch.object(dp, "process_start_time", return_value="fake-process-start")
        process_start.start()
        self.addCleanup(process_start.stop)
        process_alive = patch("os.kill", return_value=None)
        process_alive.start()
        self.addCleanup(process_alive.stop)

    def write_registry(self):
        atomic_json(self.pages / "codes.json", self.registry)

    def test_lock_excludes_another_watcher_and_releases(self):
        with replies.watcher_lock(self.directory) as first:
            self.assertTrue(first)
            with replies.watcher_lock(self.directory) as second:
                self.assertFalse(second)
            self.assertEqual(replies.watch(self.directory, poller=lambda *a, **k: self.fail("locked")), 0)
        with replies.watcher_lock(self.directory) as third:
            self.assertTrue(third)

    def test_watch_holds_lock_polls_sixty_and_exits_two_hours_after_last_terminal(self):
        terminal = NOW
        self.registry["D1"].update(state="expired", terminalAt=terminal.isoformat())
        self.write_registry()
        current = [terminal + timedelta(hours=1, minutes=58)]
        calls = []
        def poller(directory, now):
            with replies.watcher_lock(directory) as acquired:
                self.assertFalse(acquired)
            calls.append(now)
        def sleep(seconds):
            self.assertEqual(seconds, 60)
            current[0] += timedelta(seconds=seconds)
        self.assertEqual(replies.watch(self.directory, clock=lambda: current[0], sleep=sleep, poller=poller), 0)
        self.assertEqual(len(calls), 2)
        self.assertEqual(current[0], terminal + timedelta(hours=2))

    def test_watch_restarts_when_decision_code_changes(self):
        self.registry["D1"].update(state="open")
        self.write_registry()
        stamps = iter([[1], [1], [2]])
        restarted = []
        polls = []
        with patch.object(replies, "code_stamp", lambda: next(stamps)):
            result = replies.watch(self.directory, clock=lambda: NOW, sleep=lambda s: None,
                                   poller=lambda d, now: polls.append(now), restart=lambda: restarted.append(1) or 7)
        self.assertEqual((result, restarted, len(polls)), (7, [1], 1))

    def test_watch_expires_dead_or_reused_page_under_registry_lock_and_waits_two_hours(self):
        started = "Fri Oct 30 11:00:00 2026"
        for current_start in (None, "Fri Oct 30 11:01:00 2026"):
            with self.subTest(current_start=current_start):
                self.registry["D1"].update(state="open", pid=43210, pidStart=started)
                self.registry["D2"] = {"state": "decided", "pid": 999, "pidStart": started,
                                       "terminalAt": (NOW - timedelta(hours=3)).isoformat()}
                self.write_registry()
                current = [NOW]
                cycles = []

                def process_start(pid):
                    self.assertEqual(pid, 43210)
                    with (self.pages / ".state.lock").open("a") as lock:
                        with self.assertRaises(BlockingIOError):
                            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    return current_start

                def poller(directory, now):
                    registry = json.loads((self.pages / "codes.json").read_text())
                    self.assertEqual(registry["D1"]["state"], "expired")
                    self.assertEqual((registry["D1"]["by"], registry["D1"]["reason"]),
                                     ("watcher", "page-dead"))
                    self.assertEqual(replies.timestamp(registry["D1"]["terminalAt"]), NOW)
                    self.assertEqual(registry["D2"], self.registry["D2"])
                    cycles.append(now)

                def sleep(seconds):
                    self.assertEqual(seconds, 60)
                    current[0] += timedelta(seconds=seconds)

                dead = patch("os.kill", side_effect=ProcessLookupError) if current_start is None else patch("os.kill")
                with dead, patch.object(replies, "process_start_time", side_effect=process_start, create=True) as probe:
                    self.assertEqual(replies.watch(self.directory, clock=lambda: current[0],
                                                  sleep=sleep, poller=poller), 0)
                self.assertEqual(probe.call_count, 0 if current_start is None else 1)
                self.assertEqual(len(cycles), 120)
                self.assertEqual(current[0], NOW + timedelta(hours=2))

    def test_watch_checks_live_page_identity_each_cycle_before_terminal_exit(self):
        started = "Fri Oct 30 11:00:00 2026"
        self.registry["D1"].update(pid=43210, pidStart=started)
        self.write_registry()
        current = [NOW]
        cycles = []

        def poller(directory, now):
            registry = json.loads((self.pages / "codes.json").read_text())
            cycles.append(registry["D1"]["state"])
            self.assertEqual(cycles[-1], "open" if len(cycles) == 1 else "expired")
            if len(cycles) > 2:
                current[0] += timedelta(hours=2)

        def sleep(_):
            current[0] += timedelta(seconds=60)

        with patch("os.kill", side_effect=[None, ProcessLookupError]), patch.object(
                replies, "process_start_time", return_value=started, create=True) as probe:
            replies.watch(self.directory, clock=lambda: current[0], sleep=sleep, poller=poller)
        self.assertEqual(cycles, ["open", "expired", "expired"])
        self.assertEqual(probe.call_args_list, [unittest.mock.call(43210)])

    def test_watch_keeps_open_and_continues_after_page_query_failure(self):
        for error in (None, PermissionError("ps denied"), subprocess.TimeoutExpired("ps", 10)):
            with self.subTest(error=error):
                self.registry["D1"].update(state="open", pid=43210, pidStart="original-start")
                self.write_registry()
                cycles = []

                def poller(directory, now):
                    row = json.loads((self.pages / "codes.json").read_text())["D1"]
                    self.assertEqual(row["state"], "open")
                    cycles.append(now)
                    if len(cycles) == 2:
                        row.update(state="decided", terminalAt=(NOW - timedelta(hours=3)).isoformat())
                        atomic_json(self.pages / "codes.json", {"D1": row})

                with patch.object(replies, "process_start_time", side_effect=error, return_value=None):
                    replies.watch(self.directory, clock=lambda: NOW, sleep=lambda _: None, poller=poller)
                self.assertEqual(len(cycles), 2)

    def test_watch_keeps_open_and_continues_after_pid_permission_error(self):
        self.registry["D1"].update(pid=43210, pidStart="original-start")
        self.write_registry()
        cycles = []

        def poller(directory, now):
            row = json.loads((self.pages / "codes.json").read_text())["D1"]
            self.assertEqual(row["state"], "open")
            cycles.append(now)
            if len(cycles) == 2:
                row.update(state="decided", terminalAt=(NOW - timedelta(hours=3)).isoformat())
                atomic_json(self.pages / "codes.json", {"D1": row})

        with patch("os.kill", side_effect=PermissionError("pid denied")), patch.object(
                replies, "process_start_time", side_effect=PermissionError("ps denied")):
            replies.watch(self.directory, clock=lambda: NOW, sleep=lambda _: None, poller=poller)
        self.assertEqual(len(cycles), 2)

    def test_untracked_or_malformed_legacy_page_identity_is_not_expired(self):
        for fields in ({}, {"pid": 43210}, {"pidStart": "old"},
                       {"pid": True, "pidStart": "old"}, {"pid": "43210", "pidStart": "old"},
                       {"pid": 43210, "pidStart": " "}):
            with self.subTest(fields=fields):
                registry = {"D1": {"state": "open", **fields}}
                atomic_json(self.pages / "codes.json", registry)
                with patch.object(replies, "process_start_time", side_effect=AssertionError("untracked page")):
                    self.assertEqual(replies.expire_dead_pages(self.directory, NOW), registry)
                self.assertEqual(json.loads((self.pages / "codes.json").read_text()), registry)

    def test_open_code_prevents_idle_exit_and_new_terminal_resets_deadline(self):
        current = [NOW]
        calls = []
        def poller(directory, now):
            calls.append(now)
            if len(calls) == 1:
                self.registry["D1"].update(state="decided", terminalAt=(NOW + timedelta(hours=3)).isoformat())
                self.write_registry()
        def sleep(seconds):
            current[0] += timedelta(hours=3) if len(calls) == 1 else timedelta(hours=1)
        replies.watch(self.directory, clock=lambda: current[0], sleep=sleep, poller=poller)
        self.assertEqual(len(calls), 3)
        self.assertEqual(current[0], NOW + timedelta(hours=5))


if __name__ == "__main__":
    unittest.main()
