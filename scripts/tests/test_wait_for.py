"""wait-for: 조건마다 성공·시간 초과(last seen)·조기 실패, 출력 한 줄, 종료 코드, 파일을 만들지 않음.

파일 삭제가 느린 기기라 클래스 하나가 임시 폴더 하나와 파일 몇 개만 쓴다(같은 파일을 다시 쓴다)."""
import http.server
import json
import os
from pathlib import Path
import socket
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from _harness import bound  # 부하에 맞춘 하위 프로세스 한도

SCRIPT = Path(__file__).resolve().parents[1] / "wait-for"


class QuietServer(http.server.ThreadingHTTPServer):
    # HTTPServer.server_bind의 getfqdn은 CI 러너에서 35초가 걸렸다(decision_page와 같은 이유).
    def server_bind(self):
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        code = int(self.path.strip("/") or 200)
        self.send_response(code)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *args):
        pass


class WaitForChecks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.dir = Path(cls.tmp.name)
        cls.log = cls.dir / "job.log"
        cls.attempt = cls.dir / "attempt-x"
        cls.attempt.mkdir()
        cls.receipt = cls.attempt / "worker.json"

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def setUp(self):
        self.log.write_text("")
        if self.receipt.exists():
            self.receipt.write_text("{}")

    def run_wait(self, *args, delay=None, action=None):
        timer = threading.Timer(delay, action) if action else None
        started = time.monotonic()
        if timer:
            timer.start()
        try:
            result = subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True,
                                    timeout=bound(20))
        finally:
            if timer:
                timer.cancel()
        result.elapsed = time.monotonic() - started
        if result.returncode != 2:
            self.assertEqual(len(result.stdout.splitlines()), 1, result.stdout)
        return result

    def append(self, text):
        with self.log.open("a") as stream:
            stream.write(text)

    def write_receipt(self, outcome, exit_code):
        # jira-worker(worker_runtime.execute)가 끝에 한 번 쓰는 영수증과 같은 모양.
        self.receipt.write_text(json.dumps({
            "startedAt": time.time() - 5, "key": "PROJ-1", "checkpointPath": str(self.attempt / "checkpoint.json"),
            "resultPath": str(self.attempt / "stdout.json"), "usage": None, "requirementsSnapshot": None,
            "processExitCode": exit_code, "exitCode": exit_code, "outcome": outcome, "resultValid": outcome == "completed",
            "checkpointValid": True, "recoveryEvidence": None, "stageComplete": outcome == "completed",
            "stoppedServers": [], "endedAt": time.time(), "durationSeconds": 5.0}))

    # --grep

    def test_grep_met_soon_after_line_appears(self):
        result = self.run_wait("--grep", "^exit=", str(self.log), "--interval", "0.2", "--timeout", "10",
                               delay=1.0, action=lambda: self.append("working\nexit=0\n"))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertRegex(result.stdout, r"^met after \d+s: exit=0\n$")
        self.assertLess(result.elapsed, bound(1.0 + 0.2 + 1.5))  # 줄이 생긴 뒤 한 간격 안팎

    def test_grep_timeout_reports_last_seen(self):
        self.append("still working\n")
        result = self.run_wait("--grep", "^exit=", str(self.log), "--interval", "0.2", "--timeout", "1")
        self.assertEqual(result.returncode, 3)
        # 부하가 크면 마지막 확인이 늦어 2s로 반올림될 수 있다.
        self.assertRegex(result.stdout, r"^timeout after [12]s: /\^exit=/ in .+; last seen: no matching line\n$")

    def test_grep_missing_file_is_last_seen(self):
        missing = self.dir / "never.log"
        result = self.run_wait("--grep", "x", str(missing), "--interval", "0.2", "--timeout", "0.5")
        self.assertEqual(result.returncode, 3)
        self.assertIn(f"last seen: {missing} missing", result.stdout)
        self.assertFalse(missing.exists())

    def test_since_now_ignores_lines_written_before_start(self):
        self.append("exit=1 old\n")
        old = self.run_wait("--grep", "^exit=", str(self.log), "--since-now", "--interval", "0.2", "--timeout", "0.6")
        self.assertEqual(old.returncode, 3, old.stdout)
        # wait-for가 시작한(부하가 크면 1초 넘게 걸린다) 뒤에 붙인다.
        new = self.run_wait("--grep", "^exit=", str(self.log), "--since-now", "--interval", "0.2",
                            "--timeout", str(bound(10)), delay=bound(1.5), action=lambda: self.append("exit=0 new\n"))
        self.assertEqual(new.returncode, 0, new.stdout)
        self.assertIn("exit=0 new", new.stdout)

    def test_fail_grep_ends_early(self):
        result = self.run_wait("--grep", "Started App", str(self.log), "--fail-grep", "run failed|Exception",
                               str(self.log), "--interval", "0.2", "--timeout", "15",
                               delay=0.5, action=lambda: self.append("boot\nApplication run failed: port in use\n"))
        self.assertEqual(result.returncode, 4)
        self.assertRegex(result.stdout, r"^failed after \d+s: Application run failed: port in use\n$")
        self.assertLess(result.elapsed, bound(5))

    def test_earlier_line_wins_in_the_same_file(self):
        self.append("Started App in 3s\nException later\n")
        result = self.run_wait("--grep", "Started App", str(self.log), "--fail-grep", "Exception", str(self.log),
                               "--timeout", "2")
        self.assertEqual(result.returncode, 0, result.stdout)

    # 파일·프로세스·포트·URL·명령

    def test_file_and_gone(self):
        self.assertEqual(self.run_wait("--file", str(self.log), "--timeout", "1").returncode, 0)
        missing = self.dir / "none"
        result = self.run_wait("--file", str(missing), "--interval", "0.2", "--timeout", "0.5")
        self.assertEqual((result.returncode, result.stdout.split("last seen: ")[1]), (3, "missing\n"))
        self.assertEqual(self.run_wait("--gone", str(missing), "--timeout", "1").returncode, 0)
        result = self.run_wait("--gone", str(self.log), "--interval", "0.2", "--timeout", "0.5")
        self.assertEqual(result.returncode, 3)
        self.assertIn("last seen: still exists", result.stdout)

    def test_pid_gone(self):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(1)"])
        reaper = threading.Thread(target=child.wait)
        reaper.start()
        result = self.run_wait("--pid-gone", str(child.pid), "--interval", "0.2", "--timeout", "10")
        reaper.join()
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn(f"pid {child.pid} gone", result.stdout)
        result = self.run_wait("--pid-gone", str(os.getpid()), "--interval", "0.2", "--timeout", "0.5")
        self.assertEqual(result.returncode, 3)
        self.assertIn(f"last seen: pid {os.getpid()} running", result.stdout)

    def test_port(self):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            port = listener.getsockname()[1]
            self.assertEqual(self.run_wait("--port", str(port), "--timeout", "2").returncode, 0)
        result = self.run_wait("--port", str(port), "--host", "127.0.0.1", "--interval", "0.2", "--timeout", "0.5")
        self.assertEqual(result.returncode, 3)
        self.assertIn(f"port 127.0.0.1:{port}; last seen:", result.stdout)

    def test_url_status(self):
        server = QuietServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            self.assertEqual(self.run_wait("--url", base + "/404", "--timeout", "2").returncode, 0)
            result = self.run_wait("--url", base + "/503", "--interval", "0.2", "--timeout", "0.5")
            self.assertEqual(result.returncode, 3)
            self.assertIn("last seen: HTTP 503", result.stdout)
            result = self.run_wait("--url", base + "/404", "--status", "200", "--interval", "0.2", "--timeout", "0.5")
            self.assertIn("last seen: HTTP 404", result.stdout)
            self.assertEqual(self.run_wait("--url", base + "/200", "--status", "200", "--timeout", "2").returncode, 0)
        finally:
            server.shutdown()
            server.server_close()

    def test_cmd(self):
        result = self.run_wait("--cmd", "echo build 12 SUCCESS", "--timeout", "2")
        self.assertEqual(result.returncode, 0)
        self.assertIn("(build 12 SUCCESS)", result.stdout)
        result = self.run_wait("--cmd", "echo build 12 RUNNING; exit 1", "--interval", "0.2", "--timeout", "3")
        self.assertEqual(result.returncode, 3)
        self.assertIn("last seen: exit 1: build 12 RUNNING", result.stdout)

    # jira-worker 영수증

    def test_worker_completed_receipt_is_met(self):
        result = self.run_wait("--worker", str(self.attempt), "--interval", "0.2", "--timeout", "10",
                               delay=0.5, action=lambda: self.write_receipt("completed", 0))
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("worker outcome=completed exitCode=0", result.stdout)

    def test_worker_failed_or_timeout_receipt_is_failure(self):
        for outcome, code in (("failed", 1), ("timeout", 124), ("launch-failed", 127)):
            with self.subTest(outcome=outcome):
                self.write_receipt(outcome, code)
                result = self.run_wait("--worker", str(self.receipt), "--timeout", "2")
                self.assertEqual(result.returncode, 4, result.stdout)
                self.assertRegex(result.stdout, rf"^failed after \d+s: worker outcome={outcome} exitCode={code}")

    def test_worker_without_receipt_times_out(self):
        empty = self.dir  # attempt 폴더처럼 worker.json이 아직 없는 폴더
        result = self.run_wait("--worker", str(empty), "--interval", "0.2", "--timeout", "0.5")
        self.assertEqual(result.returncode, 3)
        self.assertIn(f"last seen: no receipt at {empty / 'worker.json'}", result.stdout)

    def test_worker_newer_than_skips_previous_attempt_receipt(self):
        # 재시도: <결과>.worker.json에는 이전 시도(failed) 영수증이 남아 있다. 띄우기 전 시각 뒤에 쓰인 것만 본다.
        self.write_receipt("failed", 1)
        launched = time.time()
        os.utime(self.receipt, (launched - 60, launched - 60))
        args = ("--worker", str(self.receipt), "--newer-than", str(int(launched)), "--interval", "0.2")
        stale = self.run_wait(*args, "--timeout", "0.5")
        self.assertEqual(stale.returncode, 3, stale.stdout)
        self.assertIn("is older than --newer-than", stale.stdout)
        self.write_receipt("completed", 0)  # 두 호출 사이에 끝나도 같은 명령을 다시 실행하면 잡는다
        self.assertEqual(self.run_wait(*args, "--timeout", "2").returncode, 0)

    # 사용법·부작용

    def test_usage_errors_exit_2(self):
        cases = ([], ["--file", "a", "--gone", "b"], ["--grep", "(", str(self.log)], ["--port", "1", "--status", "200"],
                 ["--file", "a", "--timeout", "0"], ["--pid-gone", "1", "--since-now"],
                 ["--grep", "x", str(self.log), "--newer-than", "1"])
        for args in cases:
            with self.subTest(args=args):
                result = self.run_wait(*args)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                self.assertEqual(result.stdout, "")

    def test_creates_no_file(self):
        before = sorted(p.name for p in self.dir.rglob("*"))
        self.run_wait("--grep", "x", str(self.dir / "absent.log"), "--fail-grep", "y", str(self.log),
                      "--interval", "0.2", "--timeout", "0.4")
        self.run_wait("--worker", str(self.dir / "absent-attempt"), "--interval", "0.2", "--timeout", "0.4")
        self.assertEqual(sorted(p.name for p in self.dir.rglob("*")), before)


if __name__ == "__main__":
    unittest.main()
