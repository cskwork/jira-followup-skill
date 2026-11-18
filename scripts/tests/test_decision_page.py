"""Offline tests for decision_page.py (stg-approval, sql-run, choice) and sql_runner.run."""

import json
import fcntl
import os
from pathlib import Path
import sys
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import urllib.error
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import decision_page as dp  # noqa: E402
import sql_runner  # noqa: E402

STG = {"kind": "stg-approval", "key": "PROJ-1389", "title": "PROJ-1389 스테이징 배포를 승인할까요?",
       "summary": "stg 준비 완료", "review": ["core-api — approved"],
       "refs": {"status": "carried", "repos": [{"repo": "core-api", "dev": True, "stg": False,
                "branches": [{"branch": "fix/PROJ-1315", "own": False, "other_keys": ["PROJ-1315"]}]}]}}

CHOICE = {"kind": "choice", "humanReason": "product-scope", "key": "PROJ-1", "title": "처리 방법", "question": "어떻게 할까요?",
          "summary": "과거 기록 | " + "가" * 400,
          "options": [{"id": "wait", "label": "기다림"}, {"id": "skip", "label": "제외"}],
          "recommended": "skip"}


class DecisionBoundary(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.log = self.directory / "decisions.jsonl"
        run = subprocess.run

        def offline_ps(argv, *args, **kwargs):
            if argv[:3] == ["ps", "-o", "lstart="]:
                return subprocess.CompletedProcess(argv, 0, "Fri Oct 30 11:00:00 2026\n")
            return run(argv, *args, **kwargs)

        self.ps = patch.object(dp.subprocess, "run", side_effect=offline_ps)
        self.ps.start()
        self.addCleanup(self.ps.stop)

    def start(self, spec, *, actions=None, wake=None):
        self.woken = []
        server, _, self.state = dp.make_server(dict(spec), actions=actions or {}, wake=wake or self.woken.append,
                                               log_path=self.log, token="tok")
        self.server = server
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        self.server_thread.start()
        self.base = f"http://127.0.0.1:{server.server_address[1]}"

    def post(self, **fields):
        data = urllib.parse.urlencode({"t": "tok", **fields}).encode()
        return urllib.request.urlopen(self.base + "/decide", data=data, timeout=5).read().decode()

    def codes(self):
        return json.loads((self.directory / "decision-pages/codes.json").read_text())

    def test_registration_records_page_pid_and_stripped_process_start(self):
        started = "Fri Oct 30 11:00:00 2026"
        with patch.object(dp.os, "getpid", return_value=43210), patch.object(
                dp.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, "  " + started + "\n")) as ps:
            code = dp.register_code(self.directory, dict(CHOICE), "page", "http://127.0.0.1:1/?t=fake")
        self.assertEqual((self.codes()[code]["pid"], self.codes()[code]["pidStart"]), (43210, started))
        self.assertEqual(ps.call_args.args[0], ["ps", "-o", "lstart=", "-p", "43210"])

    def test_registration_without_process_start_does_not_publish_open_code(self):
        with patch.object(dp.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, "")):
            with self.assertRaises(RuntimeError):
                dp.register_code(self.directory, dict(CHOICE), "page", "http://127.0.0.1:1/?t=fake")
        self.assertFalse((self.directory / "decision-pages/codes.json").exists())

    def test_concurrent_registration_is_monotonic_and_preserves_terminals(self):
        from concurrent.futures import ThreadPoolExecutor

        def register(number):
            return dp.register_code(self.directory, dict(CHOICE), f"page-{number}",
                                    f"http://127.0.0.1:1/?t=fake-{number}")

        with ThreadPoolExecutor(max_workers=4) as executor:
            codes = list(executor.map(register, range(8)))
        self.assertEqual(set(codes), {f"D{n}" for n in range(1, 9)})
        dp.update_code(self.directory, "D3", "decided", decidedBy="button")
        dp.update_code(self.directory, "D3", "expired")
        self.assertEqual(register(9), "D9")
        self.assertEqual(self.codes()["D3"]["state"], "decided")
        self.assertTrue((self.directory / "decision-pages/page-9.code").exists())

    def test_shutdown_expires_code_and_keeps_terminal_record(self):
        self.start(CHOICE)
        self.server.shutdown()
        self.server.server_close()
        self.assertTrue((self.directory / "decision-pages/codes.json").exists())
        self.assertEqual(self.codes()["D1"]["state"], "expired")

    def test_accepted_action_exception_cancels_timer_and_stops_server(self):
        sql = self.directory / "fake.sql"
        sql.write_text("SELECT 1")

        def action(*_):
            raise subprocess.TimeoutExpired("fake-sql", 1)

        self.start({"kind": "sql-run", "key": "PROJ-1", "title": "가짜 SQL",
                    "config": "fake_stg", "expect_host": "fake-stg-", "file": str(sql)},
                   actions={"sql-run": action})
        timer = self.server.timeout_timer = threading.Timer(28800, lambda: None)
        self.addCleanup(timer.cancel)
        timer.start()
        with self.assertRaises(urllib.error.HTTPError) as failed:
            self.post(option="approve")
        self.assertEqual(failed.exception.code, 500)
        self.assertIn("TimeoutExpired", failed.exception.read().decode())
        self.server_thread.join(timeout=2)
        timer.join(timeout=2)
        self.assertFalse(self.server_thread.is_alive())
        self.assertFalse(timer.is_alive())

    def test_wake_exception_does_not_leave_used_server_running(self):
        def wake(_):
            raise OSError("private details must stay local")

        self.start(CHOICE, wake=wake)
        with self.assertRaises(urllib.error.HTTPError) as failed:
            self.post(option="skip")
        text = failed.exception.read().decode()
        self.assertEqual(failed.exception.code, 500)
        self.assertNotIn("private details", text)
        self.server_thread.join(timeout=2)
        self.assertFalse(self.server_thread.is_alive())
        self.assertEqual(len(self.log.read_text().splitlines()), 1)
        self.assertEqual(self.codes()["D1"]["state"], "decided")

    def spawn_page(self, timeout=28800):
        spec = self.directory / "spec.json"
        spec.write_text(json.dumps(CHOICE))
        url_file = self.directory / "page.url"
        env = {**os.environ, "JIRA_WAKE_DIR": str(self.directory)}
        bootstrap = ("import sys; sys.path.insert(0, sys.argv.pop(1)); import decision_page as dp; "
                     "dp.process_start_time = lambda pid: 'fake-process-start'; "
                     "dp.default_actions = lambda directory: ({}, lambda key: None); "
                     "raise SystemExit(dp.main(sys.argv[1:]))")
        process = subprocess.Popen([sys.executable, "-c", bootstrap, str(ROOT), "spec", str(spec),
                                    "--url-file", str(url_file), "--timeout", str(timeout)],
                                   env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

        def stop():
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=5)

        self.addCleanup(stop)
        deadline = time.monotonic() + 5
        while (not url_file.exists() or not url_file.stat().st_size) and process.poll() is None and time.monotonic() < deadline:
            time.sleep(.02)
        if not url_file.exists():  # 진단만 더한다: 자식의 종료 코드·stderr(2026-11-10 CI에서 원인이 보이지 않았다).
            if process.poll() is None:
                process.kill()
            _, stderr = process.communicate(timeout=5)
            self.fail(f"page did not start: rc={process.returncode} stderr={stderr[-1500:]!r}")
        url = url_file.read_text().strip()
        token = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["t"][0]
        return process, url.split("?")[0], token

    def test_server_start_does_not_resolve_hostname(self):
        # 2026-11-10 GitHub macOS 러너: HTTPServer.server_bind의 socket.getfqdn('127.0.0.1')이 35초 걸려 페이지가 안 떴다.
        import socket
        def slow(*args, **kwargs):
            raise AssertionError("server start must not call socket.getfqdn")
        with patch.object(socket, "getfqdn", slow):
            server, _, _ = dp.make_server(CHOICE, actions={}, wake=lambda key: None, directory=self.directory, name="fqdn")
        try:
            self.assertEqual(server.server_name, "127.0.0.1")
        finally:
            server.server_close()

    def test_process_exits_after_decision_without_timeout_thread(self):
        process, url, token = self.spawn_page()
        data = urllib.parse.urlencode({"t": token, "option": "skip"}).encode()
        urllib.request.urlopen(url + "decide", data=data, timeout=5).close()
        stdout, stderr = process.communicate(timeout=5)
        self.assertEqual(process.returncode, 0, stdout + stderr)
        self.assertEqual(self.codes()["D1"]["state"], "decided")

    def test_timeout_process_expires_code(self):
        process, _, _ = self.spawn_page(timeout=1)
        stdout, stderr = process.communicate(timeout=5)
        self.assertEqual(process.returncode, 0, stdout + stderr)
        self.assertEqual(self.codes()["D1"]["state"], "expired")

    def test_sigterm_process_expires_code(self):
        process, _, _ = self.spawn_page()
        process.terminate()
        stdout, stderr = process.communicate(timeout=5)
        self.assertEqual(process.returncode, 0, stdout + stderr)
        self.assertEqual(self.codes()["D1"]["state"], "expired")


class DecisionRequest(unittest.TestCase):
    def test_launcher_starts_watcher_only_when_lock_is_free(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            binary = directory / "bin"
            binary.mkdir()
            calls = directory / "watcher-called"
            shim = binary / "python3"
            shim.write_text("#!" + sys.executable + "\n" + r'''
import os, pathlib, sys
if len(sys.argv) > 1 and pathlib.Path(sys.argv[1]).name == 'decision_page.py':
    pathlib.Path(sys.argv[sys.argv.index('--url-file') + 1]).write_text('http://127.0.0.1:1/?t=fake')
elif len(sys.argv) > 1 and pathlib.Path(sys.argv[1]).name == 'decision_replies.py':
    pathlib.Path(os.environ['FAKE_WATCHER_CALLS']).write_text('started')
else:
    os.execv(os.environ['FAKE_REAL_PYTHON'], [os.environ['FAKE_REAL_PYTHON'], *sys.argv[1:]])
''')
            shim.chmod(0o755)
            spec = directory / "spec.json"
            spec.write_text(json.dumps(CHOICE))
            pages = directory / "decision-pages"
            pages.mkdir()
            env = {**os.environ, "JIRA_WAKE_DIR": str(directory), "FAKE_WATCHER_CALLS": str(calls),
                   "FAKE_REAL_PYTHON": sys.executable, "PATH": str(binary) + os.pathsep + os.environ["PATH"]}
            argv = ["/bin/bash", str(ROOT / "decision-request"), "spec", str(spec)]
            with (pages / ".replies.lock").open("a") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                held = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=10)
                self.assertEqual(held.returncode, 0, held.stdout + held.stderr)
                self.assertFalse(calls.exists())
            free = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=10)
            self.assertEqual(free.returncode, 0, free.stdout + free.stderr)
            deadline = time.monotonic() + 5
            while not calls.exists() and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertTrue(calls.exists())


class Page(unittest.TestCase):
    def start(self, spec, ok=True):
        self.tmp = tempfile.TemporaryDirectory()
        self.log = Path(self.tmp.name) / "decisions.jsonl"
        self.done, self.woken = [], []

        def action(spec_, stamp):
            self.done.append((spec_["kind"], stamp))
            return ok, "runner output"

        actions = {"stg-approval": action, "sql-run": action}
        with patch.object(dp, "process_start_time", return_value="fake-process-start"):
            self.server, _, self.state = dp.make_server(spec, actions=actions, wake=self.woken.append,
                                                       log_path=self.log, token="tok")
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def post(self, path, **fields):
        data = urllib.parse.urlencode({"t": "tok", **fields}).encode()
        return urllib.request.urlopen(self.base + path, data=data, timeout=10).read().decode()

    def audit(self):
        return [json.loads(l) for l in self.log.read_text().splitlines()] if self.log.exists() else []

    def test_token_gate_and_facts(self):
        self.start(STG)
        with self.assertRaises(urllib.error.HTTPError) as denied:
            urllib.request.urlopen(self.base + "/?t=bad", timeout=5)
        self.assertEqual(denied.exception.code, 403)
        text = urllib.request.urlopen(self.base + "/?t=tok", timeout=5).read().decode()
        for needle in ("PROJ-1389", "fix/PROJ-1315", "approved", "승인 (stg 배포 진행)"):
            self.assertIn(needle, text)

    def test_stg_approve_runs_action_once_audits_and_wakes(self):
        self.start(STG)
        self.assertIn("승인·실행 완료", self.post("/decide", option="approve"))
        self.assertEqual([k for k, _ in self.done], ["stg-approval"])
        self.assertEqual(self.woken, ["PROJ-1389"])
        self.assertEqual(self.audit()[0]["option"], "approve")
        self.assertTrue(self.audit()[0]["ok"])

    def test_failed_sql_run_does_not_wake(self):
        sql = Path(tempfile.mkdtemp()) / "m.sql"
        sql.write_text("SELECT 1;\n")
        self.start({"kind": "sql-run", "key": "PROJ-1389", "title": "스테이징 DB 변경 실행",
                    "config": "app_stg", "expect_host": "stg-db", "file": str(sql)}, ok=False)
        page = urllib.request.urlopen(self.base + "/?t=tok", timeout=5).read().decode()
        self.assertIn("SELECT 1;", page)
        self.assertIn("실패", self.post("/decide", option="approve"))
        self.assertEqual(self.woken, [])
        self.assertFalse(self.audit()[0]["ok"])

    def test_choice_records_option_and_hold_records_nothing(self):
        spec = {"kind": "choice", "humanReason": "product-scope", "key": "PROJ-1389", "title": "TC-25 처리", "question": "어떻게 할까요?",
                "options": [{"id": "wait", "label": "기획 답 기다림"}, {"id": "skip", "label": "이번 배포 제외"}],
                "recommended": "skip", "recommendReason": "기획 답 없이 배포 일정이 먼저 온다"}
        self.start(spec)
        page = urllib.request.urlopen(self.base + "/?t=tok", timeout=5).read().decode()
        self.assertIn("AI 권장</span> <b>이번 배포 제외</b> — 기획 답 없이", page)
        self.assertEqual(page.count("AI 권장"), 2)  # 요약 1곳 + 권장 버튼 1곳
        with self.assertRaises(urllib.error.HTTPError) as bad:
            self.post("/decide", option="nope")
        self.assertEqual(bad.exception.code, 400)
        self.assertIn("이번 배포 제외", self.post("/decide", option="skip"))
        self.assertEqual((self.audit()[0]["option"], self.audit()[0]["recommended"]), ("skip", "skip"))
        self.server.shutdown()
        self.start(spec)
        self.assertIn("보류", self.post("/hold"))
        self.assertEqual(self.audit(), [])


class Recommendation(unittest.TestCase):
    def test_choice_requires_recommendation(self):
        spec = {"kind": "choice", "humanReason": "product-scope", "key": "PROJ-1", "title": "t", "question": "q",
                "options": [{"id": "a", "label": "A"}], "recommended": "zzz"}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "spec.json"
            path.write_text(json.dumps(spec))
            with self.assertRaises(SystemExit):
                dp.main(["spec", str(path)])
        legacy = {**spec, "recommended": None, "default": "a"}
        self.assertEqual(dp.recommendation(legacy)["id"], "a")


class FakeCursor:
    def __init__(self, host, guard=0, fail_on=None):
        self.host, self.guard, self.fail_on, self.executed, self.description, self._rows = host, guard, fail_on, [], None, []

    def execute(self, sql):
        self.executed.append(sql)
        if self.fail_on and self.fail_on in sql:
            raise RuntimeError("boom")
        if sql == "SELECT @@hostname":
            self.description, self._rows = [("h",)], [(self.host,)]
        elif sql.startswith("SELECT COUNT"):
            self.description, self._rows = [("n",)], [(self.guard,)]
        else:
            self.description, self._rows, self.rowcount = None, [], 3

    def fetchone(self):
        return self._rows[0]

    def fetchall(self):
        return self._rows


class FakeConn:
    def __init__(self, cursor):
        self.c, self.committed, self.rolled = cursor, False, False

    def cursor(self):
        return self.c

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled = True


class Runner(unittest.TestCase):
    SQL = "-- header\nSELECT @@hostname, VERSION();\nALTER TABLE t ADD COLUMN c int;\nUPDATE t SET c = 1;\n"

    def test_wrong_host_refuses_without_running(self):
        cur = FakeCursor("dev-dbv2-001-4g42")
        result = sql_runner.run(FakeConn(cur), expect_host="stg-db", sql_text=self.SQL)
        self.assertEqual(result["status"], "refused")
        self.assertEqual(cur.executed, ["SELECT @@hostname"])

    def test_guard_mismatch_refuses(self):
        cur = FakeCursor("stg-db001-2881", guard=1)
        result = sql_runner.run(FakeConn(cur), expect_host="stg-db", sql_text=self.SQL,
                                guard_sql="SELECT COUNT(*) FROM x", guard_expect=0)
        self.assertEqual(result["status"], "refused")
        self.assertNotIn("ALTER TABLE t ADD COLUMN c int", cur.executed)

    def test_runs_in_order_skips_comments_and_commits(self):
        cur = FakeCursor("stg-db001-2881")
        conn = FakeConn(cur)
        result = sql_runner.run(conn, expect_host="stg-db", sql_text=self.SQL,
                                guard_sql="SELECT COUNT(*) FROM x", guard_expect=0)
        self.assertEqual(result["status"], "done")
        self.assertEqual(cur.executed[2:], ["SELECT @@hostname, VERSION()", "ALTER TABLE t ADD COLUMN c int", "UPDATE t SET c = 1"])
        self.assertTrue(conn.committed)

    def test_stops_on_first_error_and_rolls_back(self):
        cur = FakeCursor("stg-db001-2881", fail_on="ALTER")
        conn = FakeConn(cur)
        result = sql_runner.run(conn, expect_host="stg-db", sql_text=self.SQL)
        self.assertEqual(result["status"], "failed")
        self.assertNotIn("UPDATE t SET c = 1", cur.executed)
        self.assertTrue(conn.rolled)


if __name__ == "__main__":
    unittest.main()
