"""Offline tests for decision_page.py (stg-approval, sql-run, choice) and sql_runner.run."""

import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
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


class Page(unittest.TestCase):
    def start(self, spec, ok=True):
        self.tmp = tempfile.TemporaryDirectory()
        self.log = Path(self.tmp.name) / "decisions.jsonl"
        self.done, self.woken = [], []

        def action(spec_, stamp):
            self.done.append((spec_["kind"], stamp))
            return ok, "runner output"

        actions = {"stg-approval": action, "sql-run": action}
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
        spec = {"kind": "choice", "key": "PROJ-1389", "title": "TC-25 처리", "question": "어떻게 할까요?",
                "options": [{"id": "wait", "label": "기획 답 기다림"}, {"id": "skip", "label": "이번 배포 제외"}]}
        self.start(spec)
        with self.assertRaises(urllib.error.HTTPError) as bad:
            self.post("/decide", option="nope")
        self.assertEqual(bad.exception.code, 400)
        self.assertIn("이번 배포 제외", self.post("/decide", option="skip"))
        self.assertEqual(self.audit()[0]["option"], "skip")
        self.server.shutdown()
        self.start(spec)
        self.assertIn("보류", self.post("/hold"))
        self.assertEqual(self.audit(), [])


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
