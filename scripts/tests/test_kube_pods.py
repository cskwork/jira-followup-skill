"""공용 kube 경로·정규식·환경 재개는 주입한 명령 실행기로만 검증한다."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import re
import subprocess
import sys
import unittest
from unittest.mock import Mock, patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
import preflight_env

ERRORS = (
    ("wrong-vpn: VPN not on stg", "vpn", "VPN"),
    ("WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED!", "host-key", "지문"),
    ("Host key verification failed.", "host-key", "지문"),
    ("The connection to the server localhost:8080 was refused", "kube-auth", "kubeconfig"),
    ("kubectl: Unauthorized", "kube-auth", "인증"),
    ("Error from server (Forbidden): pods is forbidden", "kube-auth", "인증"),
    ("iam-authenticator configure failed", "kube-auth", "인증"),
    ("Permission denied (publickey)", "ssh-auth", "SSH"),
)


CONNECT = "/opt/kube/connect.sh"


class KubePodChecks(unittest.TestCase):
    def setUp(self):
        script = SCRIPTS / "kube_pods.py"
        self.assertTrue(script.is_file(), "공용 kube_pods.py가 필요하다")
        spec = importlib.util.spec_from_file_location("kube_pods_test", script)
        self.kube = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.kube)
        self.kube.CONNECT = CONNECT  # KUBE_CONNECT_CMD 설정, 호스트는 기본값(dev·stg)
        self.now = 0.0
        self.sleep = Mock(side_effect=self.advance)

    def advance(self, seconds):
        self.now += seconds

    def probe(self, *args, **kwargs):
        with patch("time.monotonic", lambda: self.now), patch("time.sleep", self.sleep):
            return self.kube.probe(*args, **kwargs)

    def timed_runner(self, *outcomes):
        steps = iter(outcomes)

        def command(name, argv, timeout):
            elapsed, value = next(steps)
            self.advance(elapsed)
            if isinstance(value, Exception):
                raise value
            return value

        return Mock(side_effect=command)

    def runner(self, output="pod/web-app-stg-abc\n", error="", code=0):
        return Mock(return_value=subprocess.CompletedProcess([], code, output, error))

    def cli(self, *args, runner):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                patch("time.monotonic", lambda: self.now), patch("time.sleep", self.sleep):
            code = self.kube.main(list(args), command=runner)
        return code, out.getvalue(), err.getvalue()

    def test_both_envs_use_canonical_readonly_builder_and_bounded_timeout(self):
        for env in ("stg", "dev"):
            for timeout, expected in ((20, 20), (60, 30), (3.5, 3.5)):
                with self.subTest(env=env, timeout=timeout):
                    runner = self.runner("pod/web-app-one\npod/web-app-one\npod/core-api-two\n")
                    value = self.kube.probe(env, timeout=timeout, command=runner)
                    self.assertEqual((value["status"], value["reason"]), ("PASS", "ok"))
                    self.assertEqual(value["pods"], ["core-api-two", "web-app-one"])
                    runner.assert_called_once_with(env + "-kube", [
                        CONNECT, env, "kubectl get pods -A -o name"], expected)

    def test_probe_cli_prints_json_only_after_listing(self):
        code, output, error = self.cli("probe", "--env", "stg", runner=self.runner())
        self.assertEqual((code, error), (0, ""))
        self.assertEqual(json.loads(output)["pods"], ["web-app-stg-abc"])

    def test_default_probe_timeout_is_thirty_seconds(self):
        runner = self.runner()
        self.kube.probe("dev", command=runner)
        self.assertEqual(runner.call_args.args[-1], 30)

    def test_both_modes_preserve_each_failure_classification_and_fix_hint(self):
        for message, reason, hint in ERRORS:
            for mode in ("probe", "regex"):
                with self.subTest(message=message, mode=mode):
                    runner = self.runner("", message + " sensitive-fixture-value", 1)
                    args = [mode, "--env", "stg"]
                    if mode == "regex":
                        args += ["--service", "web-app"]
                    code, output, error = self.cli(*args, runner=runner)
                    self.assertNotEqual(code, 0)
                    self.assertNotIn("sensitive-fixture-value", output + error)
                    if mode == "probe":
                        value = json.loads(output)
                        self.assertEqual(value["reason"], reason)
                        self.assertIn(hint, value["hint"])
                    else:
                        self.assertEqual(output, "")
                        self.assertIn(reason, error)
                        self.assertIn(hint, error)

    def test_unavailable_command_and_subprocess_errors_remain_other_in_both_modes(self):
        for failure in (subprocess.SubprocessError("fixture"), FileNotFoundError("fixture")):
            for mode in ("probe", "regex"):
                with self.subTest(failure=type(failure).__name__, mode=mode):
                    runner = Mock(side_effect=failure)
                    args = [mode, "--env", "dev"]
                    if mode == "regex":
                        args += ["--service", "web-app"]
                    code, output, error = self.cli(*args, runner=runner)
                    self.assertNotEqual(code, 0)
                    value = json.loads(output) if mode == "probe" else {"reason": error}
                    self.assertIn("other", value["reason"])
                    self.assertIn("SSH", output + error)

    def test_timeouts_have_own_reason_and_korean_hint_in_both_modes(self):
        for mode in ("probe", "regex"):
            with self.subTest(mode=mode):
                runner = self.timed_runner((30, subprocess.TimeoutExpired(["sensitive-fixture-value"], 30)))
                args = [mode, "--env", "dev"]
                if mode == "regex":
                    args += ["--service", "core-api"]
                code, output, error = self.cli(*args, runner=runner)
                self.assertEqual(code, 1)
                if mode == "probe":
                    value = json.loads(output)
                    self.assertEqual(value["reason"], "timeout")
                    self.assertEqual(value["pods"], [])
                else:
                    self.assertEqual(output, "")
                    self.assertTrue(error.startswith("timeout: "), error)
                self.assertIn("SSH/VPN 응답이 30초 안에 오지 않음", output + error)
                self.assertIn("다시 시도하거나 VPN 상태 확인", output + error)
                self.assertNotIn("sensitive-fixture-value", output + error)

    def test_timeout_hint_uses_the_smaller_caller_limit(self):
        runner = self.timed_runner((2.5, subprocess.TimeoutExpired(["fixture"], 2.5)))
        value = self.probe("stg", timeout=2.5, command=runner)
        self.assertEqual(value["reason"], "timeout")
        self.assertIn("2.5초", value["hint"])
        self.assertEqual(runner.call_args.args[-1], 2.5)

    def ssh_rejection(self):
        return subprocess.CompletedProcess([], 255, "", "deploy@192.0.2.10: Permission denied "
                                           "(publickey,gssapi-keyex,gssapi-with-mic,password).")

    def test_transient_ssh_auth_retries_once_with_remaining_budget(self):
        runner = self.timed_runner((0.3, self.ssh_rejection()), (0.7, self.runner().return_value))
        value = self.probe("dev", command=runner)
        self.assertEqual((value["status"], value.get("attempts")), ("PASS", 2))
        self.assertEqual(value["pods"], ["web-app-stg-abc"])
        self.assertEqual(runner.call_count, 2)
        self.assertEqual(runner.call_args_list[0].args[-1], 30)
        self.assertAlmostEqual(runner.call_args_list[1].args[-1], 27.7)
        self.assertEqual(runner.call_args_list[0].args[:2], runner.call_args_list[1].args[:2])
        self.sleep.assert_called_once_with(2)

    def test_repeated_ssh_auth_reports_ssh_auth_after_exactly_two_attempts(self):
        runner = self.timed_runner((0.3, self.ssh_rejection()), (0.3, self.ssh_rejection()))
        value = self.probe("dev", command=runner)
        self.assertEqual((value["status"], value["reason"], value.get("attempts")),
                         ("FAIL", "ssh-auth", 2))
        self.assertEqual(value["hint"], "SSH 비밀번호 인증이 일시 거부됨; 반복되면 계정 잠김·비밀번호 만료 확인")
        self.assertEqual(runner.call_count, 2)
        self.sleep.assert_called_once_with(2)

    def test_non_retryable_failures_do_not_sleep_or_retry(self):
        for message, reason, _ in ERRORS:
            if reason == "ssh-auth":
                continue
            with self.subTest(reason=reason):
                self.sleep.reset_mock()
                runner = self.runner("", message, 1)
                value = self.probe("stg", command=runner)
                self.assertEqual(value["reason"], reason)
                self.assertEqual(value.get("attempts", 1), 1)
                runner.assert_called_once()
                self.sleep.assert_not_called()

    def test_timeout_can_retry_within_larger_caller_budget_and_thirty_second_cap(self):
        runner = self.timed_runner((30, subprocess.TimeoutExpired(["fixture"], 30)),
                                   (1, self.runner().return_value))
        value = self.probe("dev", timeout=60, command=runner)
        self.assertEqual((value["status"], value.get("attempts")), ("PASS", 2))
        self.assertEqual([call.args[-1] for call in runner.call_args_list], [30, 28])
        self.sleep.assert_called_once_with(2)

    def test_second_failure_reports_its_reason_without_a_third_attempt(self):
        for second, reason in ((self.runner("", "kubectl: Unauthorized", 1).return_value, "kube-auth"),
                               (subprocess.TimeoutExpired(["fixture"], 27.7), "timeout"),
                               (FileNotFoundError("fixture"), "other")):
            with self.subTest(reason=reason):
                self.now = 0.0
                self.sleep.reset_mock()
                runner = self.timed_runner((0.3, self.ssh_rejection()), (0.5, second))
                value = self.probe("dev", command=runner)
                self.assertEqual((value["status"], value["reason"], value.get("attempts")),
                                 ("FAIL", reason, 2))
                self.assertEqual(runner.call_count, 2)
                self.sleep.assert_called_once_with(2)

    def test_exhausted_budget_does_not_sleep_or_retry(self):
        for timeout, elapsed, response, reason in (
                (2, 0, self.ssh_rejection(), "ssh-auth"),
                (3, 1, self.ssh_rejection(), "ssh-auth"),
                (30, 30, subprocess.TimeoutExpired(["fixture"], 30), "timeout")):
            with self.subTest(timeout=timeout, elapsed=elapsed):
                self.now = 0.0
                self.sleep.reset_mock()
                runner = self.timed_runner((elapsed, response))
                value = self.probe("dev", timeout=timeout, command=runner)
                self.assertEqual(value["reason"], reason)
                self.assertEqual(value.get("attempts", 1), 1)
                runner.assert_called_once()
                self.sleep.assert_not_called()

    def test_budget_is_rechecked_after_sleep_before_retry(self):
        self.sleep.side_effect = lambda seconds: self.advance(30)
        runner = self.timed_runner((0.3, self.ssh_rejection()))
        value = self.probe("dev", command=runner)
        self.assertEqual(value["reason"], "ssh-auth")
        self.assertEqual(value.get("attempts", 1), 1)
        runner.assert_called_once()
        self.sleep.assert_called_once_with(2)

    def test_both_cli_modes_share_the_retry_path(self):
        for mode in ("probe", "regex"):
            with self.subTest(mode=mode):
                self.now = 0.0
                self.sleep.reset_mock()
                response = self.runner("replicaset.apps/core-api-6d6975965b\n"
                                       "pod/core-api-6d6975965b-chvl2\n" if mode == "regex"
                                       else "pod/core-api-6d6975965b-chvl2\n").return_value
                runner = self.timed_runner((0.3, self.ssh_rejection()), (0.5, response))
                args = [mode, "--env", "dev"] + (["--service", "core-api"] if mode == "regex" else [])
                code, output, error = self.cli(*args, runner=runner)
                self.assertEqual((code, error), (0, ""))
                if mode == "probe":
                    self.assertEqual(json.loads(output)["attempts"], 2)
                else:
                    self.assertIsNotNone(re.search(output.strip(), "core-api-6d6975965b-abc12"))
                self.assertEqual(runner.call_count, 2)
                self.sleep.assert_called_once_with(2)

    def test_empty_or_partially_invalid_listing_never_passes(self):
        for output in ("", "\n", "No resources found", "pod/good-name\nnot-a-pod\n",
                       "pod/UPPERCASE\n", "pod/name/extra\n", "pod/web-app-a\npod/a..b\n",
                       "pod/web-app-a\npod/a.-b\n", "pod/web-app-a\npod/a-.b\n"):
            for mode in ("probe", "regex"):
                with self.subTest(output=output, mode=mode):
                    args = [mode, "--env", "stg"]
                    if mode == "regex":
                        args += ["--service", "web-app"]
                    code, out, error = self.cli(*args, runner=self.runner(output))
                    self.assertNotEqual(code, 0)
                    self.assertIn("other", out + error)
                    if mode == "regex":
                        self.assertEqual(out, "")

    def test_nonzero_exit_with_valid_pod_output_still_fails(self):
        value = self.kube.probe("stg", command=self.runner(code=1))
        self.assertEqual((value["status"], value["reason"]), ("FAIL", "other"))

    def test_regex_fallback_warns_and_is_limited_to_exact_observed_service_pods(self):
        runner = self.runner("pod/web-app-z.1\npod/web-app-a\npod/web-app-a\n"
                             "pod/web-app2-a\npod/core-api-a\n")
        code, output, error = self.cli("regex", "--env", "stg", "--service", "web-app", runner=runner)
        self.assertEqual(code, 0)
        self.assertIn("경고", error)
        self.assertIn("ReplicaSet", error)
        self.assertIn("baseline", error)
        self.assertIn("불완전", error)
        expression = output.strip()
        self.assertTrue(expression.startswith("^") and expression.endswith("$"))
        rx = re.compile(expression)
        for name in ("web-app-z.1", "web-app-a"):
            self.assertIsNotNone(rx.search(name))
        for name in ("web-app2-a", "core-api-a", "prefix-web-app-a",
                     "web-app-a-suffix", "web-app-zX1", "web-app-unobserved"):
            self.assertIsNone(rx.search(name), name)

    def test_regex_covers_old_and_current_replicasets_in_only_the_selected_cluster(self):
        for env in ("dev", "stg"):
            with self.subTest(env=env):
                runner = self.runner("replicaset.apps/core-api-6d6975965b\n"
                                     "replicaset.apps/core-api-5b7c8d9f66\n"
                                     "replicaset.apps/core-api-6d6975965b\n"
                                     "replicaset.apps/core-sse-api-6d6975965b\n"
                                     "replicaset.apps/core-api-extra-7d8c9f\n"
                                     "pod/core-api-6d6975965b-chvl2\n")
                code, output, error = self.cli("regex", "--env", env, "--service", "core-api", runner=runner)
                self.assertEqual((code, error), (0, ""))
                self.assertEqual(output.strip(), r"^core\-api-(?:5b7c8d9f66|6d6975965b)-[a-z0-9]{5}$")
                rx = re.compile(output.strip())
                for name in ("core-api-5b7c8d9f66-abc12", "core-api-6d6975965b-chvl2"):
                    self.assertIsNotNone(rx.search(name), name)
                for name in ("core-sse-api-6d6975965b-chvl2", "core-api-7f8c9d6b55-chvl2",
                             "core-api-extra-7d8c9f-abc12", "core-api-6d6975965b-abcd",
                             "core-api-6d6975965b-abcdef", "core-api-6d6975965b-ABCD1",
                             "prefix-core-api-6d6975965b-chvl2", "core-api-6d6975965b-chvl2-extra"):
                    self.assertIsNone(rx.search(name), name)
                runner.assert_called_once_with(env + "-kube", [
                    CONNECT, env, "kubectl get rs,pods -A -o name"], 30)

    def test_matching_replicaset_without_current_pods_still_produces_regex(self):
        code, output, error = self.cli("regex", "--env", "dev", "--service", "core-api",
                                       runner=self.runner("replicaset.apps/core-api-6d6975965b\n"))
        self.assertEqual((code, error), (0, ""))
        self.assertIsNotNone(re.search(output.strip(), "core-api-6d6975965b-abc12"))

    def test_unrelated_replicaset_uses_current_pod_fallback(self):
        code, output, error = self.cli("regex", "--env", "dev", "--service", "core-api",
                                       runner=self.runner("replicaset.apps/core-api-extra-abc123\n"
                                                          "pod/core-api-6d6975965b-chvl2\n"))
        self.assertEqual(code, 0)
        self.assertIn("baseline", error)
        self.assertIsNotNone(re.search(output.strip(), "core-api-6d6975965b-chvl2"))
        self.assertIsNone(re.search(output.strip(), "core-api-6d6975965b-abc12"))

    def test_no_matching_service_pods_fails_without_regex(self):
        code, output, error = self.cli("regex", "--env", "stg", "--service", "web-app",
                                       runner=self.runner("pod/core-api-a\n"))
        self.assertNotEqual(code, 0)
        self.assertEqual(output, "")
        self.assertIn("other", error)
        self.assertIn("web-app", error)

    def test_invalid_service_is_rejected_before_any_command(self):
        for service in ("", "web-app;whoami", "web-app|.*", "-web-app", "UPPER"):
            runner = self.runner()
            with self.subTest(service=service), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as caught:
                    self.kube.main(["regex", "--env", "stg", "--service", service], command=runner)
                self.assertEqual(caught.exception.code, 2)
                runner.assert_not_called()


class KubePreflightChecks(unittest.TestCase):
    def environment(self, need, *, vpn="stg", output="pod/web-app-a\n", error="", code=0,
                    vpn_error=False, fail_fast=False):
        calls = []

        def command(name, argv, timeout):
            calls.append((name, argv, timeout))
            if name == "vpn":
                active = [] if vpn == "none" else [{"profile": vpn, "state": "CONNECTED"}]
                return subprocess.CompletedProcess(argv, int(vpn_error), json.dumps({"active": active}), "")
            self.assertTrue(name.endswith("-kube"), name)
            return subprocess.CompletedProcess(argv, code, output, error)

        self.assertIn(need, preflight_env.NEEDS)
        connect = patch.object(preflight_env.kube_pods, "CONNECT", CONNECT)
        connect.start()
        self.addCleanup(connect.stop)
        with patch("time.sleep"):
            value, result = preflight_env.check_environment([need] + (["jira"] if fail_fast else []),
                                                            command=command, fail_fast=fail_fast)
        return value, result, calls

    def test_both_kube_needs_list_pods_after_required_vpn(self):
        for env in ("stg", "dev"):
            with self.subTest(env=env):
                value, code, calls = self.environment(env + "-kube", vpn=env)
                self.assertEqual((value["status"], code), ("PASS", 0))
                self.assertTrue(value["results"][0]["required"])
                self.assertEqual([call[0] for call in calls], ["vpn", env + "-kube"])
                self.assertEqual(calls[-1][2], 20)
                self.assertEqual(value["results"][-1]["observed"], 1)

    def test_wrong_vpn_never_invokes_ssh(self):
        for need, vpn in (("stg-kube", "dev"), ("dev-kube", "stg"), ("stg-kube", "none")):
            with self.subTest(need=need, vpn=vpn):
                value, code, calls = self.environment(need, vpn=vpn)
                self.assertEqual((value["status"], code), ("HUMAN", 10))
                row = value["results"][-1]
                self.assertEqual(row["reason"], "vpn")
                self.assertIn(need.split("-")[0], row["human_action"])
                self.assertEqual([call[0] for call in calls], ["vpn"])

    def test_failures_keep_reason_and_action_in_environment_wait(self):
        for message, reason, hint in ERRORS:
            with self.subTest(message=message):
                value, code, calls = self.environment("stg-kube", output="", error=message, code=1)
                self.assertNotEqual(code, 0)
                self.assertEqual(value["results"][-1]["reason"], reason)
                self.assertIn(hint, value["results"][-1]["human_action"])

    def test_vpn_diagnostic_failure_is_required_and_fail_fast(self):
        value, code, calls = self.environment("stg-kube", vpn_error=True, fail_fast=True)
        self.assertEqual((value["status"], code), ("HUMAN", 10))
        self.assertTrue(value["results"][0]["required"])
        self.assertEqual([row["status"] for row in value["results"]], ["FAIL", "SKIP", "SKIP"])
        self.assertEqual([call[0] for call in calls], ["vpn"])

    def test_stg_units_declare_kube_and_preserve_current_extra_needs(self):
        for step, expected in (("stg-smoke", "stg-web,stg-kube"),
                               ("stg-e2e", "stg-web,stg-kube"),
                               ("stg-comment-prep", "stg-kube")):
            with self.subTest(step=step):
                try:
                    row = preflight_env.next_step_item({"stage": "evidence", "nextStep": step,
                        "needs": "dev-db", "waiting": "env-human", "stepNeeds": "stg-kube,jira"})
                except ValueError as error:
                    self.fail(str(error))
                self.assertEqual(row["needs"], expected + ",jira")
                self.assertEqual(row["waiting"], "env-human")

    def test_runbook_declares_stg_units_and_uses_shared_regex_builder(self):
        runbook = (SCRIPTS.parent / "RUNBOOK.md").read_text()
        for step in ("stg-smoke", "stg-e2e", "stg-comment-prep"):
            self.assertIn(step, runbook)
        self.assertIn("kube_pods.py regex --env stg --service <svc>", runbook)
        self.assertNotIn("정규식은 stg 클러스터 `kubectl get pods` 이름으로 만든다", runbook)


if __name__ == "__main__":
    unittest.main()
