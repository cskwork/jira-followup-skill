"""49735: 관찰 가능한 공백은 재현 전에 질문으로 넘기지 않는다."""

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import followup_triage as triage


class IntentGateTest(unittest.TestCase):
    def test_49735_observable_and_planning_gaps_reproduce_before_question(self):
        event = {"key": "PROJ-1405", "intake": True, "clarity": 75,
                 "observable": ["재현 조건"], "planning": ["역할별 기대 동작"]}
        with tempfile.TemporaryDirectory() as directory:
            logs = Path(directory)
            result = triage.intent_gate(event, logs=logs)
            self.assertEqual((result["stage"], result["question_allowed"]), ("reproduce", False))
            (logs / "asis-attempts.md").write_text("  \n")
            self.assertFalse(triage.intent_gate(event, logs=logs)["question_allowed"])
            (logs / "asis-attempts.md").write_text("조건 | 결과 | 증적\n첫 진입 | 무한 로딩 | asis.png\n")
            self.assertTrue(triage.intent_gate(event, logs=logs)["question_allowed"])

    def test_operational_event_and_intake_follow_same_rule(self):
        for context in ({"intake": True}, {"operationalIssue": True}):
            result = triage.intent_gate({**context, "clarity": 75, "observable": ["영향 범위"], "planning": []})
            self.assertEqual((result["stage"], result["question_allowed"]), ("reproduce", False))

    def test_planning_only_can_ask_but_cannot_build(self):
        result = triage.intent_gate({"clarity": 75, "observable": [], "planning": ["미응시 안내"]})
        self.assertEqual((result["stage"], result["question_allowed"], result["build_allowed"]),
                         ("intent", True, False))

    def test_owner_choice_never_waives_clarity(self):
        result = triage.intent_gate({"clarity": 75, "observable": [], "planning": [], "choice": "fix-in-1405"})
        self.assertFalse(result["build_allowed"])
        result = triage.intent_gate({"clarity": 95, "observable": [], "planning": []})
        self.assertTrue(result["build_allowed"])

    def test_question_cli_is_read_only_and_fails_before_reproduction(self):
        with tempfile.TemporaryDirectory() as directory:
            context = Path(directory) / "intent-gate.json"
            context.write_text(json.dumps({"clarity": 75, "observable": ["사용자 상태"], "planning": []}))
            response = subprocess.run([sys.executable, str(ROOT / "followup_triage.py"),
                                       "--check-intent", str(context), "--question", "--logs", directory],
                                      capture_output=True, text=True, timeout=10)
            self.assertEqual(response.returncode, 1, response.stderr)
            self.assertEqual(json.loads(response.stdout)["stage"], "reproduce")
            self.assertEqual(list(Path(directory).iterdir()), [context])

    def test_missing_classification_is_an_error(self):
        with self.assertRaises(ValueError):
            triage.intent_gate({"clarity": 75})


if __name__ == "__main__":
    unittest.main()
