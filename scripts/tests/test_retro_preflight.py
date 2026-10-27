import contextlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "preflight_env.py"
SPEC = importlib.util.spec_from_file_location("retro_preflight", SCRIPT)
preflight = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(preflight)


class RetroPreflightTests(unittest.TestCase):
    def run_preflight(self, needs, *, vpn="dev", fail_fast=False, http_status=200, vpn_error=False):
        self.commands = []
        self.http_calls = []

        def command(name, argv, timeout):
            self.commands.append(name)
            if name == "vpn":
                active = [] if vpn == "none" else [{"profile": vpn, "state": "CONNECTED"}]
                return subprocess.CompletedProcess(argv, int(vpn_error), json.dumps({"active": active}), "")
            if name.endswith("-db"):
                payload = {"rows": [{"h": vpn + "-db-fixture"}]}
            else:
                payload = []
            return subprocess.CompletedProcess(argv, 0, json.dumps(payload), "")

        def http(url, timeout):
            self.http_calls.append(url)
            return http_status

        arguments = ["--need", needs]
        if fail_fast:
            arguments.append("--fail-fast")
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = preflight.main(arguments, command=command, http=http)
        return code, json.loads(output.getvalue())

    def test_wrong_vpn_never_invokes_db_command(self):
        for vpn, need in (("dev", "stg-db"), ("stg", "dev-db"), ("none", "dev-db")):
            with self.subTest(vpn=vpn, need=need):
                code, data = self.run_preflight(need, vpn=vpn)
                self.assertEqual(self.commands, ["vpn"])
                self.assertEqual((code, data["status"]), (10, "HUMAN"))
                row = data["results"][-1]
                self.assertEqual((row["status"], row["reason"], row["observed"]),
                                 ("FAIL", "wrong-vpn", None))
                self.assertIn("OTP", row["human_action"])

    def test_correct_vpn_still_probes_db_hostname(self):
        code, data = self.run_preflight("dev-db")
        self.assertEqual(self.commands, ["vpn", "dev-db"])
        self.assertEqual((code, data["status"]), (0, "PASS"))

    def test_fail_fast_human_failure_skips_jenkins_and_remaining_probes(self):
        code, data = self.run_preflight("stg-db,jenkins-dev,jira,dev-web", fail_fast=True)
        self.assertEqual(self.commands, ["vpn"])
        self.assertEqual(self.http_calls, [])
        self.assertEqual((code, data["status"]), (10, "HUMAN"))
        self.assertEqual([row["name"] for row in data["results"]],
                         ["vpn", "stg-db", "jenkins-dev", "jira", "dev-web"])
        for row in data["results"][2:]:
            self.assertEqual((row["status"], row["reason"]), ("SKIP", "fail-fast"))
            self.assertIsNone(row["observed"])
            self.assertIsNone(row["human_action"])

    def test_fail_fast_down_failure_preserves_exit_twenty(self):
        code, data = self.run_preflight("dev-web,jenkins-dev", fail_fast=True, http_status=503)
        self.assertEqual(self.commands, ["vpn"])
        self.assertEqual(len(self.http_calls), 1)
        self.assertEqual((code, data["status"]), (20, "DOWN"))
        self.assertEqual(data["results"][-1]["status"], "SKIP")

    def test_fail_fast_all_pass_preserves_exit_zero(self):
        code, data = self.run_preflight("dev-db,jenkins-dev", fail_fast=True)
        self.assertEqual(self.commands, ["vpn", "dev-db", "jenkins-dev"])
        self.assertEqual((code, data["status"]), (0, "PASS"))
        self.assertTrue(all(row["status"] == "PASS" for row in data["results"]))

    def test_fail_fast_required_vpn_failure_stops_all_probes(self):
        code, data = self.run_preflight("dev-db,jenkins-dev", fail_fast=True, vpn_error=True)
        self.assertEqual(self.commands, ["vpn"])
        self.assertEqual((code, data["status"]), (10, "HUMAN"))
        self.assertEqual([row["status"] for row in data["results"]], ["FAIL", "SKIP", "SKIP"])

    def test_public_checks_ignore_nonrequired_vpn_diagnostic_failure(self):
        code, data = self.run_preflight("dev-web,jenkins-dev", fail_fast=True, vpn_error=True)
        self.assertEqual(self.commands, ["vpn", "jenkins-dev"])
        self.assertEqual(len(self.http_calls), 1)
        self.assertEqual((code, data["status"]), (0, "PASS"))
        self.assertFalse(data["results"][0]["required"])

    def test_without_fail_fast_remaining_checks_still_run(self):
        code, data = self.run_preflight("dev-web,jenkins-dev", http_status=503)
        self.assertEqual(self.commands, ["vpn", "jenkins-dev"])
        self.assertEqual((code, data["status"]), (20, "DOWN"))
        self.assertEqual(data["results"][-1]["status"], "PASS")


if __name__ == "__main__":
    unittest.main()
