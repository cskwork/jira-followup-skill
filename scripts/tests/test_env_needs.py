"""다음 동작에 필요한 needs만 남기고 사람 대기를 보존한다."""

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from preflight_env import next_step_item


class NextStepNeedsTest(unittest.TestCase):
    def item(self, **extra):
        return {"stage": "build", "waiting": "env-human", "needs": "dev-db,jira,jenkins-dev",
                "note": "preserve", **extra}

    def test_established_cause_local_data_drops_future_remote_needs(self):
        original = self.item()
        result = next_step_item(original)
        self.assertEqual((result["needs"], result["waiting"], result["nextStep"]), ("", None, "local"))
        self.assertEqual(original["needs"], "dev-db,jira,jenkins-dev")
        self.assertEqual(result["note"], "preserve")

    def test_web_only_reproduction_needs_public_dev_web(self):
        result = next_step_item(self.item(stage="reproduce", nextStep="reproduce-web"))
        self.assertEqual(result["needs"], "dev-web")
        result = next_step_item(self.item(stage="reproduce", nextStep="reproduce-web", profile="stg"))
        self.assertEqual(result["needs"], "stg-web")

    def test_db_reproduction_still_waits_on_actual_profile(self):
        result = next_step_item(self.item(stage="reproduce", nextStep="reproduce-db", profile="stg"))
        self.assertEqual((result["needs"], result["waiting"]), ("stg-db", "env-human"))

    def test_deploy_and_e2e_do_not_require_db(self):
        self.assertEqual(next_step_item(self.item(nextStep="dev-deploy"))["needs"], "jenkins-dev")
        self.assertEqual(next_step_item(self.item(nextStep="dev-e2e"))["needs"], "dev-web")
        self.assertEqual(next_step_item(self.item(nextStep="jira"))["needs"], "jira")

    def test_unknown_stage_preserves_requirements_and_human_risk_is_not_released(self):
        original = self.item(stage="intent")
        self.assertEqual(next_step_item(original), original)
        result = next_step_item(self.item(waiting="human-risk"))
        self.assertEqual(result["waiting"], "human-risk")
        for waiting in ("reporter", "stg-approval"):
            self.assertEqual(next_step_item(self.item(waiting=waiting))["waiting"], waiting)

    def test_local_without_established_cause_is_rejected(self):
        with self.assertRaises(ValueError):
            next_step_item(self.item(stage="reproduce", nextStep="local"))
        result = next_step_item(self.item(stage="intent", nextStep="local", causeEstablished=True))
        self.assertEqual(result["needs"], "")


if __name__ == "__main__":
    unittest.main()
