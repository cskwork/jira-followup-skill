"""Offline CLI contracts and environment failure fixtures (stdlib only)."""

import contextlib
import http.client
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("preflight_env", ROOT / "preflight_env.py")
preflight = importlib.util.module_from_spec(spec)
spec.loader.exec_module(preflight)


class GitFixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name) / "repo"
        self.repo.mkdir()
        self.env = {**os.environ, "GIT_AUTHOR_NAME": "Verifier Test", "GIT_AUTHOR_EMAIL": "test@example.invalid",
                    "GIT_COMMITTER_NAME": "Verifier Test", "GIT_COMMITTER_EMAIL": "test@example.invalid",
                    "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
        self.git("init", "-b", "main")
        self.base = self.commit("production baseline")
        self.git("update-ref", "refs/remotes/origin/main", self.base)
        self.git("checkout", "-b", "ticket")

    def git(self, *args):
        result = subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-C", str(self.repo), *args],
                                capture_output=True, text=True, env=self.env, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def commit(self, subject):
        self.git("commit", "--allow-empty", "-m", subject)
        return self.git("rev-parse", "HEAD")

    def cli(self, script, *args):
        response = subprocess.run([sys.executable, str(ROOT / script), *map(str, args)],
                                  capture_output=True, text=True, env=self.env, timeout=15)
        self.assertEqual(response.stderr, "")
        self.assertEqual(len(response.stdout.splitlines()), 1, response.stdout)
        return response.returncode, json.loads(response.stdout)


class BaseChecks(GitFixture):
    def check(self, *args):
        return self.cli("check_base.py", "--repo", self.repo, "--branch", "ticket", "--key", "PROJ-1388", *args)

    def test_clean_ticket_pass(self):
        self.commit("fix(core-api): 학급 선택에 다른 학기 거름 [PROJ-1388]")
        code, data = self.check()
        self.assertEqual((code, data["status"]), (0, "PASS"))
        self.assertEqual(data["merge_base"], self.base)
        self.assertEqual(data["base_head"], self.base)
        self.assertEqual(data["behind"], 0)
        self.assertTrue(data["commits"][0]["ok"])

    def test_foreign_key_fail_then_explicit_allow_pass(self):
        self.commit("fix(core-api): 학급 선택 수정 [PROJ-1388]")
        self.commit("fix(core-api): 연계 티켓 수정 [PROJ-1389]")
        code, data = self.check()
        self.assertEqual((code, data["status"]), (1, "FAIL"))
        self.assertEqual(sum(not c["ok"] for c in data["commits"]), 1)
        self.assertTrue(next(c["reason"] for c in data["commits"] if not c["ok"]))
        code, data = self.check("--also-key", "PROJ-1389")
        self.assertEqual((code, data["status"]), (0, "PASS"))

    def test_merge_commit_fails_even_with_ticket_subject(self):
        self.commit("fix(core-api): 학급 선택 수정 [PROJ-1388]")
        self.git("checkout", "-b", "side", self.base)
        self.commit("fix(core-api): 추가 수정 [PROJ-1388]")
        self.git("checkout", "ticket")
        self.git("merge", "--no-ff", "side", "-m", "merge [PROJ-1388]")
        code, data = self.check()
        self.assertEqual((code, data["status"]), (1, "FAIL"))
        self.assertFalse(data["commits"][0]["ok"])
        self.assertIn("merge", data["commits"][0]["reason"])

    def test_behind_is_only_informational(self):
        self.commit("fix(core-api): 학급 선택 수정 [PROJ-1388]")
        self.git("checkout", "main")
        production = self.commit("production advanced")
        self.git("update-ref", "refs/remotes/origin/main", production)
        code, data = self.check()
        self.assertEqual((code, data["status"], data["behind"]), (0, "PASS", 1))
        self.assertEqual(data["base_head"], production)

    def test_invalid_ref_is_json_git_error(self):
        code, data = self.check("--base", "missing-ref")
        self.assertEqual((code, data["status"]), (2, "FAIL"))


class DeployChecks(GitFixture):
    def setUp(self):
        super().setUp()
        self.expect = self.commit("fix(core-api): 학급 선택 수정 [PROJ-1388]")
        self.checkout = self.commit("fix(core-api): 배포에 포함된 다음 변경 [PROJ-1388]")

    def check(self, *, status="completed", result="SUCCESS", checkout=None, **extra):
        fixture = Path(self.temp.name) / "run.json"
        fixture.write_text(json.dumps({"number": 662, "status": status, "result": result,
                                       "scm": {"branch": "refs/remotes/origin/dev",
                                               "commit": self.checkout if checkout is None else checkout}, **extra}))
        return self.cli("check_deploy.py", "--job", "content-web-dev", "--build", "662",
                        "--repo", self.repo, "--expect", self.expect, "--run-json", fixture)

    def test_success_containing_expect_passes(self):
        code, data = self.check()
        self.assertEqual((code, data["status"], data["contains_expect"]), (0, "PASS", True))
        self.assertEqual(data["expect_sha"], self.expect)
        self.assertEqual(data["checkout_commit"], self.checkout)

    def test_success_older_checkout_fails(self):
        code, data = self.check(checkout=self.base)
        self.assertEqual((code, data["status"], data["contains_expect"]), (1, "FAIL", False))

    def test_running_fails_not_completed(self):
        code, data = self.check(status="running", checkout="", result=None)
        self.assertEqual((code, data["reason"]), (1, "not-completed"))

    def test_failure_result_fails(self):
        code, data = self.check(result="FAILURE")
        self.assertEqual((code, data["status"], data["contains_expect"]), (1, "FAIL", False))

    def test_unknown_checkout_fails_without_fetch(self):
        code, data = self.check(checkout="f" * 40)
        self.assertEqual((code, data["reason"]), (1, "checkout-commit-not-in-repo"))

    def test_missing_checkout_fails(self):
        code, data = self.check(checkout="")
        self.assertEqual((code, data["status"]), (1, "FAIL"))

    def test_checkout_must_be_actual_sha_not_local_ref(self):
        for checkout in ("HEAD", "ticket", "secret-token"):
            with self.subTest(checkout=checkout):
                code, data = self.check(checkout=checkout)
                self.assertEqual((code, data["reason"]), (1, "checkout-commit-invalid"))
                self.assertIsNone(data["checkout_commit"])

    def test_live_command_uses_dev_context_without_global_mutation(self):
        spec = importlib.util.spec_from_file_location("check_deploy", ROOT / "check_deploy.py")
        deploy = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(deploy)
        calls = []

        def command(argv, timeout):
            calls.append(argv)
            if argv[0] == "jk":
                self.assertEqual(argv, ["jk", "--context", "dev", "run", "view", "content-web-dev", "662", "--json"])
                return subprocess.CompletedProcess(argv, 0, json.dumps({"number": 662, "status": "completed", "result": "SUCCESS", "scm": {"commit": self.checkout}}))
            self.assertEqual(argv[0], "git")
            return subprocess.run(argv, capture_output=True, text=True, env=self.env, timeout=timeout)

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = deploy.main(["--job", "content-web-dev", "--build", "662", "--repo", str(self.repo), "--expect", self.expect], command_runner=command)
        self.assertEqual((code, json.loads(output.getvalue())["status"]), (0, "PASS"))
        self.assertEqual(sum(c[0] == "jk" for c in calls), 1)

    def test_malformed_json_is_usage_error(self):
        fixture = Path(self.temp.name) / "invalid.json"
        fixture.write_text("[]")
        code, data = self.cli("check_deploy.py", "--job", "test", "--build", "662", "--repo", self.repo,
                              "--expect", self.expect, "--run-json", fixture)
        self.assertEqual((code, data["status"]), (2, "FAIL"))


class PreflightChecks(unittest.TestCase):
    def check(self, needs, *, vpn="dev", host="dev-dbv2-001-4g42", ai=200, errors=None, outputs=None):
        self.calls = []
        errors = errors or {}
        outputs = outputs or {}

        def command(name, argv, timeout):
            self.calls.append((name, argv))
            self.assertEqual(timeout, preflight.JK_TIMEOUT if name == "jenkins-dev" else 2)
            if name in errors:
                error = errors[name]
                if isinstance(error, Exception):
                    raise error
                stderr = "secret-token"
                if isinstance(error, tuple):
                    error, stderr = error
                return subprocess.CompletedProcess(argv, error, "secret-token", stderr)
            if name in outputs:
                return subprocess.CompletedProcess(argv, 0, outputs[name], "secret-token")
            if name == "vpn":
                active = vpn if isinstance(vpn, list) else ([] if vpn == "none" else [{"profile": vpn, "state": "CONNECTED", "ignored": "secret-token"}])
                output = {"active": active}
            elif name.endswith("-db"):
                self.assertEqual(argv[-1], "SELECT @@hostname h")
                self.assertIn(argv[-2], ("app_dev", "app_stg"))
                output = {"columns": ["h"], "rows": [{"h": host}]}
            elif name == "jira":
                self.assertEqual(argv, ["acli", "jira", "auth", "status"])
                output = {"auth": "secret-token"}
            elif name == "jenkins-dev":
                self.assertEqual(argv, ["jk", "--context", "dev", "job", "ls", "--json"])
                output = []
            else:
                self.fail("Unexpected command " + name)
            return subprocess.CompletedProcess(argv, 0, json.dumps(output), "secret-token")

        def http(url, timeout):
            self.calls.append(("http", url))
            self.assertIn(url, [*preflight.AI_URLS.values(), *preflight.WEB_URLS.values()])
            self.assertEqual(timeout, 2)
            if isinstance(ai, Exception):
                raise ai
            return ai

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = preflight.main(["--need", needs, "--timeout", "2"], command=command, http=http)
        self.assertNotIn("secret-token", output.getvalue())
        self.assertEqual(len(output.getvalue().splitlines()), 1)
        self.assertEqual(self.calls[0][0], "vpn")
        return code, json.loads(output.getvalue())

    def result(self, data, name):
        return next(r for r in data["results"] if r["name"] == name)

    def test_dev_vpn_audit_db_wrong_vpn_skips_db_and_needs_human(self):
        code, data = self.check("stg-db")
        row = self.result(data, "stg-db")
        self.assertEqual((code, data["status"], row["status"]), (10, "HUMAN", "FAIL"))
        self.assertIsNone(row["observed"])
        self.assertEqual(row["reason"], "wrong-vpn")
        self.assertIn("stg.command", row["human_action"])
        self.assertIn("OTP", row["human_action"])
        self.assertNotIn("stg-db", [name for name, _ in self.calls])

    def test_dev_db_ai_all_good_pass(self):
        code, data = self.check("dev-db,dev-ai")
        self.assertEqual((code, data["status"], data["vpn"]), (0, "PASS", "dev"))
        self.assertTrue(all(r["status"] == "PASS" for r in data["results"]))

    def test_audit_web_503_is_down_without_vpn_requirement(self):
        # 2026-10-21 PROJ-1405: 스테이징 웹 503(본문 0B)인데 점검 항목이 없어 가드가 자동 재개하지 못했다.
        code, data = self.check("stg-web", vpn="dev", ai=503)
        row = self.result(data, "stg-web")
        self.assertEqual((code, data["status"], row["observed"]), (20, "DOWN", 503))
        self.assertIsNone(row["human_action"])
        self.assertIn(("http", preflight.WEB_URLS["stg"]), self.calls)
        code, data = self.check("dev-web", vpn="none")
        self.assertEqual((code, self.result(data, "dev-web")["status"]), (0, "PASS"))

    def test_dev_ai_503_is_down(self):
        code, data = self.check("dev-ai", ai=503)
        self.assertEqual((code, data["status"]), (20, "DOWN"))
        self.assertEqual(self.result(data, "dev-ai")["observed"], 503)
        self.assertIsNone(self.result(data, "dev-ai")["human_action"])

    def test_db_error_with_correct_vpn_is_down(self):
        code, data = self.check("dev-db", errors={"dev-db": 1})
        self.assertEqual((code, data["status"]), (20, "DOWN"))

    def test_auth_failure_needs_human(self):
        code, data = self.check("jira", errors={"jira": 1})
        self.assertEqual((code, data["status"]), (10, "HUMAN"))
        self.assertEqual(self.result(data, "jira")["human_action"], preflight.cfg.JIRA_REAUTH_HINT)

    def test_read_only_auth_and_jenkins_probes(self):
        code, data = self.check("jira,jenkins-dev", vpn="none")
        self.assertEqual((code, data["status"]), (0, "PASS"))

    def test_public_checks_do_not_require_a_working_vpn_command(self):
        for needs in ("jira", "jenkins-dev", "dev-web", "stg-web"):
            with self.subTest(needs=needs):
                code, data = self.check(needs, errors={"vpn": 1})
                self.assertEqual((code, data["status"]), (0, "PASS"))
                self.assertFalse(self.result(data, "vpn")["required"])
                self.assertEqual(self.result(data, "vpn")["reason"], "command-failed")

    def test_public_service_failure_is_not_masked_by_vpn_failure(self):
        code, data = self.check("dev-web", errors={"vpn": 1}, ai=503)
        self.assertEqual((code, data["status"]), (20, "DOWN"))

    def test_no_vpn_needs_human_without_ai_request(self):
        code, data = self.check("dev-ai", vpn="none")
        self.assertEqual((code, data["status"]), (10, "HUMAN"))
        self.assertNotIn("http", [name for name, _ in self.calls])

    def test_network_timeout_is_down(self):
        code, data = self.check("dev-ai", ai=TimeoutError("secret-token"))
        self.assertEqual((code, data["status"]), (20, "DOWN"))

    def test_malformed_http_response_is_down(self):
        code, data = self.check("dev-ai", ai=http.client.BadStatusLine("secret-token"))
        self.assertEqual((code, data["status"]), (20, "DOWN"))

    def test_vpn_command_timeout_needs_human(self):
        code, data = self.check("dev-ai", errors={"vpn": subprocess.TimeoutExpired("vpn", 2)})
        self.assertEqual((code, data["status"]), (10, "HUMAN"))

    def test_audit_vpn_and_actual_audit_host_pass(self):
        code, data = self.check("stg-db,stg-ai", vpn="stg", host="stg-db001")
        self.assertEqual((code, data["status"], data["vpn"]), (0, "PASS", "stg"))

    def test_audit_vpn_with_dev_hostname_still_needs_human(self):
        code, data = self.check("stg-db", vpn="stg", host="dev-dbv2-001-4g42")
        row = self.result(data, "stg-db")
        self.assertEqual((code, row["status"], row["reason"]), (10, "FAIL", "hostname-mismatch"))
        self.assertEqual(row["observed"], "dev-dbv2-001-4g42")
        self.assertIn("stg-db", [name for name, _ in self.calls])
        self.assertIn("OTP", row["human_action"])

    def test_simultaneous_vpns_need_human(self):
        code, data = self.check("dev-ai", vpn=[{"profile": p, "state": "CONNECTED"} for p in ("dev", "stg")])
        self.assertEqual((code, data["status"]), (10, "HUMAN"))
        self.assertEqual(self.result(data, "vpn")["reason"], "vpn-conflict")

    def test_malformed_vpn_json_needs_human(self):
        code, data = self.check("dev-ai", outputs={"vpn": "not json secret-token"})
        self.assertEqual((code, data["status"]), (10, "HUMAN"))

    def test_malformed_hostname_never_passes_or_leaks(self):
        code, data = self.check("dev-db", host="dev-db secret-token")
        self.assertNotEqual(code, 0)
        self.assertIsNone(self.result(data, "dev-db")["observed"])

    def test_jenkins_service_error_is_down(self):
        code, data = self.check("jenkins-dev", errors={"jenkins-dev": (1, "503 Service Unavailable")})
        self.assertEqual((code, data["status"]), (20, "DOWN"))

    def test_jenkins_auth_error_needs_human(self):
        code, data = self.check("jenkins-dev", errors={"jenkins-dev": (1, "401 Unauthorized secret-token")})
        self.assertEqual((code, data["status"]), (10, "HUMAN"))

    def test_jenkins_missing_context_needs_human(self):
        code, data = self.check("jenkins-dev", errors={"jenkins-dev": (1, 'context "dev" not found')})
        self.assertEqual((code, data["status"]), (10, "HUMAN"))

    def test_malformed_jenkins_response_is_down(self):
        code, data = self.check("jenkins-dev", outputs={"jenkins-dev": "<html>secret-token</html>"})
        self.assertEqual((code, data["status"]), (20, "DOWN"))

    def test_human_failure_takes_precedence_over_down(self):
        code, data = self.check("dev-ai,jira", ai=503, errors={"jira": 1})
        self.assertEqual((code, data["status"]), (10, "HUMAN"))


class CliChecks(unittest.TestCase):
    def test_help_and_usage_json(self):
        for script in ("check_base.py", "check_deploy.py", "preflight_env.py"):
            with self.subTest(script=script):
                response = subprocess.run([sys.executable, str(ROOT / script), "--help"], capture_output=True, text=True, timeout=10)
                self.assertEqual(response.returncode, 0, response.stderr)
                self.assertIn("usage:", response.stdout.lower())
                response = subprocess.run([sys.executable, str(ROOT / script)], capture_output=True, text=True, timeout=10)
                self.assertEqual(response.returncode, 2)
                self.assertEqual(len(response.stdout.splitlines()), 1)
                json.loads(response.stdout)


if __name__ == "__main__":
    unittest.main()
