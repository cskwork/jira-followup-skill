"""개발 단계=sdlc, 배포 단계=큐 delivery(사용자 결정 2026-11-16). 가짜 sdlc.sh(fixtures/fake-sdlc-kit)만 쓴다."""
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import followup_idle as idle  # noqa: E402
import lifecycle  # noqa: E402
from preflight_env import next_step_item  # noqa: E402
import worker_runtime  # noqa: E402

FAKE_KIT = Path(__file__).resolve().parent / "fixtures/fake-sdlc-kit"


def item(stage="plan", waiting=None, slug="proj-1-x", **extra):
    return dict(slug=slug, stage=stage, service=None, branch=None, commentIds=[], waiting=waiting, note="n", **extra)


class FakeSdlc(unittest.TestCase):
    """실제 sdlc-kit 대신 가짜 sdlc.sh를 SDLC_KIT로 준다(안전망의 JIRA_SDLC=off를 이 시험에서만 지운다)."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.dir = Path(temp.name)
        (self.dir / "workspace").mkdir()
        self.states, self.ships = {}, {}
        env = {"SDLC_KIT": str(FAKE_KIT), "JIRA_SDLC_ROOT": str(self.dir / "workspace"), "JIRA_SDLC": "on",
               "FAKE_SDLC_STATE": str(self.dir / "sdlc-state.json"), "FAKE_SDLC_SHIP": str(self.dir / "sdlc-ship.json"),
               "FAKE_SDLC_LOG": str(self.dir / "sdlc-calls.log")}
        patcher = patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)

    def sdlc(self, **states):
        self.states.update(states)
        (self.dir / "sdlc-state.json").write_text(json.dumps(self.states))

    def ship(self, **values):
        self.ships.update(values)
        (self.dir / "sdlc-ship.json").write_text(json.dumps(self.ships))

    def calls(self):
        path = self.dir / "sdlc-calls.log"
        return path.read_text().splitlines() if path.exists() else []


class Mapping(unittest.TestCase):
    def test_legacy_stage_maps_to_delivery(self):
        cases = {"intent": "none", "reproduce": "none", "plan": "none", "build": "none", "tobe": "none",
                 "evidence": "none", "dev-deploy": "none", "dev-deployed": "dev-deployed",
                 "dev-deployed-api": "dev-deployed", "dev-e2e": "dev-verified", "commented": "result-posted",
                 "result-posted": "result-posted", "dev-qa-requested": "result-posted", "stg-prepare": "stg-ready",
                 "stg-ready": "stg-ready", "stg-merged": "stg-merged", "stg-resolved": "stg-merged",
                 "stg-deployed": "stg-deployed", "stg-e2e": "stg-deployed", "stg-smoked": "stg-deployed",
                 "stg-verified": "stg-verified", "stg-commented": "stg-commented",
                 "stg-qa-requested": "stg-commented", "shipped": "shipped", "closed": "shipped",
                 "approved": None, "merge-resolved": None, None: None}
        for stage, expected in cases.items():
            self.assertEqual(lifecycle.delivery({"stage": stage}), expected, stage)
            self.assertIn(expected, (None, *lifecycle.DELIVERY))

    def test_explicit_delivery_wins_and_must_be_closed_value(self):
        self.assertEqual(lifecycle.delivery({"stage": "shipped", "delivery": "none"}), "none")
        with self.assertRaises(ValueError):
            lifecycle.delivery({"stage": "plan", "delivery": "dev-e2e"})

    def test_ship_units(self):
        for stage in ("push", "pushed", "dev-deploy", "dev-deployed", "dev-deployed-api"):
            self.assertTrue(lifecycle.is_ship_unit(stage), stage)
        for stage in ("build", "tobe", "stg-ready", "dev-e2e", None):
            self.assertFalse(lifecycle.is_ship_unit(stage), stage)


class Guard(FakeSdlc):
    def setUp(self):
        super().setUp()
        self.statuses, self.notifications = {}, []
        self.addCleanup(patch.stopall)
        patch("lessons.open_request").start()
        patch.object(idle, "run", side_effect=self.command).start()
        patch.object(idle, "notify", side_effect=lambda text, **kw: self.notifications.append(text) or True).start()
        patch.object(idle, "blocked_by", return_value=[]).start()
        patch.object(idle, "reconcile_reasons", return_value=[]).start()

    def command(self, argv):
        if "--out" in argv:
            Path(argv[argv.index("--out") + 1]).write_text(json.dumps(
                {"events": [], "errors": [], "queueStatuses": self.statuses}))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def write_queue(self, queue):
        (self.dir / "followup-queue.json").write_text(json.dumps(queue))

    def queue(self):
        return json.loads((self.dir / "followup-queue.json").read_text())

    def decide(self):
        return idle.decide(self.dir, ["scan"], ["preflight"])

    def test_sdlc_done_or_delivery_shipped_leaves_the_queue(self):
        self.write_queue({"PROJ-1": item(waiting="reporter", slug="proj-1-done"),
                          "PROJ-2": item(stage="build", waiting="reporter", slug=None, delivery="shipped"),
                          "PROJ-3": item(waiting="reporter", slug="proj-3-open")})
        self.sdlc(**{"proj-1-done": {"phase": "done", "state": "DONE"},
                     "proj-3-open": {"phase": "plan", "state": "READY"}})
        result, code = self.decide()
        self.assertEqual(code, 0)
        self.assertEqual(sorted(self.queue()), ["PROJ-3"])
        self.assertEqual(result["queueReconcile"]["removed"], {"PROJ-1": "sdlc=DONE", "PROJ-2": "delivery=shipped"})

    def test_needs_human_blocks_only_before_dev_deploy(self):
        # 1428 실제 상태: 큐는 result-posted인데 sdlc intent NEEDS-HUMAN → 막지 않고 지연만 보고한다.
        self.write_queue({"PROJ-1": item(stage="plan", slug="proj-1-human"),
                          "PROJ-2": item(stage="result-posted", slug="proj-2-human")})
        self.sdlc(**{"proj-1-human": {"phase": "intent", "state": "NEEDS-HUMAN"},
                     "proj-2-human": {"phase": "intent", "state": "NEEDS-HUMAN"}})
        result, code = self.decide()
        self.assertEqual(code, 1)
        self.assertEqual(result["reasons"], ["sdlc-human:PROJ-1", "sdlc-lag:PROJ-2", "queue-runnable:PROJ-2"])
        # 개발 단계 사람 차례만이면 깨우지 않고, 반복돼도 stuck으로 세지 않는다.
        self.write_queue({"PROJ-1": item(stage="plan", slug="proj-1-human")})
        for _ in range(2):
            result, code = self.decide()
            self.assertEqual((code, result["reasons"], result["idle"]), (0, ["sdlc-human:PROJ-1"], True))
        self.assertEqual(self.notifications, [])

    def test_lagging_sdlc_after_delivery_is_report_only(self):
        # 1437·1501·1505: 큐는 stg-deployed·stg-commented인데 sdlc는 evidence READY.
        self.write_queue({"PROJ-1": item(stage="stg-deployed", waiting="reporter", slug="proj-1-lag"),
                          "PROJ-2": item(stage="stg-commented", waiting="reporter", slug="proj-2-ok",
                                        delivery="stg-commented")})
        self.sdlc(**{"proj-1-lag": {"phase": "evidence", "state": "READY"},
                     "proj-2-ok": {"phase": "deliver", "state": "READY"}})
        result, code = self.decide()
        self.assertEqual((code, result["reasons"]), (0, ["sdlc-lag:PROJ-1"]))
        self.assertEqual(set(self.queue()), {"PROJ-1", "PROJ-2"})

    def test_sdlc_error_is_report_only_and_keeps_todays_behaviour(self):
        self.write_queue({"PROJ-1": item(slug="proj-1-missing"), "PROJ-2": item(slug="proj-2-bad", waiting="reporter")})
        self.sdlc(**{"proj-2-bad": "error"})
        result, code = self.decide()
        self.assertEqual(code, 1)
        self.assertEqual(result["reasons"], ["sdlc-error:PROJ-1", "queue-runnable:PROJ-1", "sdlc-error:PROJ-2"])
        self.write_queue({"PROJ-2": item(slug="proj-2-bad", waiting="reporter")})
        result, code = self.decide()
        self.assertEqual((code, result["reasons"]), (0, ["sdlc-error:PROJ-2"]))

    def test_sdlc_timeout_is_sdlc_error(self):
        self.write_queue({"PROJ-1": item(slug="proj-1-slow", waiting="reporter")})
        self.sdlc(**{"proj-1-slow": "sleep"})
        with patch.object(lifecycle, "TIMEOUT", 0.5):
            result, code = self.decide()
        self.assertEqual((code, result["reasons"]), (0, ["sdlc-error:PROJ-1"]))

    def test_one_status_call_per_slug_per_tick_and_none_without_slug(self):
        self.write_queue({"PROJ-1": item(slug="proj-1-x"), "PROJ-2": item(slug=None)})
        self.sdlc(**{"proj-1-x": {"phase": "build", "state": "READY"}})
        result, code = self.decide()
        self.assertEqual((code, result["reasons"]), (1, ["queue-runnable:PROJ-1", "queue-runnable:PROJ-2"]))
        self.assertEqual(self.calls(), ["status proj-1-x --json"])

    def test_off_switch_skips_sdlc(self):
        self.write_queue({"PROJ-1": item(slug="proj-1-human")})
        self.sdlc(**{"proj-1-human": {"phase": "intent", "state": "NEEDS-HUMAN"}})
        with patch.dict(os.environ, {"JIRA_SDLC": "off"}):
            result, code = self.decide()
        self.assertEqual((code, result["reasons"]), (1, ["queue-runnable:PROJ-1"]))
        self.assertEqual(self.calls(), [])


class NextStep(unittest.TestCase):
    def test_sdlc_phase_establishes_cause_before_delivery(self):
        base = {"stage": "intent", "waiting": "env-human", "needs": "dev-db"}
        self.assertEqual(next_step_item(base), base)
        result = next_step_item(base, phase="evidence")
        self.assertEqual((result["nextStep"], result["needs"], result["waiting"]), ("local", "", None))
        # 배포 뒤에는 기본 local이 없다. 원인 확정은 delivery dev-deployed·dev-verified로 남는다.
        after = {"stage": "x", "delivery": "dev-verified", "waiting": "env-human", "needs": "dev-db"}
        self.assertEqual(next_step_item(after, phase="evidence"), after)
        self.assertEqual(next_step_item(dict(after, nextStep="local"))["needs"], "")
        with self.assertRaises(ValueError):
            next_step_item({"stage": "x", "delivery": "stg-ready", "nextStep": "local"}, phase="deliver")


class ShipGate(FakeSdlc):
    def manifest(self, stage="dev-deployed", **environment):
        env = {"key": "PROJ-1", "slug": "proj-1-x", **environment}
        return {"key": "PROJ-1", "stage": stage, "checks": [
            {"name": "sdlc-ship", "artifact": "check-ship.json", "resultField": "result", "environment": env}]}

    def test_ship_unit_requires_declared_check_and_open_gate(self):
        self.ship(**{"proj-1-x": "open", "proj-1-y": "code changed after QA: HEAD differs from Commit"})
        self.assertEqual(worker_runtime.ship_gate_errors(self.manifest()), [])
        self.assertEqual(worker_runtime.ship_gate_errors(self.manifest(stage="build", slug="proj-1-y")), [])
        closed = worker_runtime.ship_gate_errors(self.manifest(slug="proj-1-y"))
        self.assertEqual(len(closed), 1)
        self.assertIn("code changed after QA", closed[0])
        missing = {"key": "PROJ-1", "stage": "dev-deploy", "checks": [
            {"name": "build", "artifact": "b.json", "resultField": "result", "environment": {"key": "PROJ-1"}}]}
        self.assertIn("sdlc-ship", worker_runtime.ship_gate_errors(missing)[0])
        self.assertIn("sdlc-ship", worker_runtime.ship_gate_errors(self.manifest(key="PROJ-2"))[0])
        self.assertEqual(self.calls(), ["check ship proj-1-x", "check ship proj-1-y"])

    def test_check_ship_cli_reports_human_risk_contract(self):
        self.ship(**{"proj-1-x": "open", "proj-1-y": "route is build"})
        out = self.dir / "check-ship.json"
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(lifecycle.main(["check-ship", "--key", "PROJ-1", "--slug", "proj-1-x", "--out", str(out)]), 0)
        self.assertEqual(json.loads(out.read_text())["result"], "PASS")
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            self.assertEqual(lifecycle.main(["check-ship", "--key", "PROJ-1", "--slug", "proj-1-y"]), 1)
        report = json.loads(stdout.getvalue())
        self.assertEqual((report["result"], report["queueWaiting"], report["key"], report["slug"]),
                         ("FAIL", "human-risk", "PROJ-1", "proj-1-y"))
        self.assertTrue(worker_runtime.human_ask_complete(report))
        self.assertNotIn("\n", report["question"] + report["suggest"])


if __name__ == "__main__":
    unittest.main()
