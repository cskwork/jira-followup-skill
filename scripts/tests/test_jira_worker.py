"""Worker prompts use only fixed local policy documents; paseo is a stub."""
import json
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

SCRIPTS = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("runbook_section", SCRIPTS / "runbook_section.py")
SECTION = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SECTION)


class WorkerSectionChecks(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.skill = self.root / "skill with spaces"
        self.scripts = self.skill / "scripts"
        self.scripts.mkdir(parents=True)
        for name in ("jira-worker", "runbook_section.py", "run_lock.py", "followup_config.py", "worker-result.schema.json"):
            if (SCRIPTS / name).exists():
                shutil.copy2(SCRIPTS / name, self.scripts / name)
        self.runbook = self.skill / "RUNBOOK.md"
        self.runbook.write_text(
            "# Runbook\n## 0. Authority\nauthority-body\n" +
            "".join(f"## {number}. Morning\nmorning-{number}-body\n" for number in range(1, 7)) +
            "## 7. Work\nwork-intro\n### 7-3. Ticket\nticket-body\n" +
            "".join(f"{number}. **Step**: work-{number}\n" for number in range(7)) +
            "### 7-3-C. Conflict\nconflict-body\n### 7-4. Other\nother-body\n")
        (self.skill / "ORCHESTRATOR.md").write_text(
            "# Orchestrator\n## 0. Boundary\norchestrator-boundary\n"
            "## 2. Assignment\norchestrator-assignment\n## 3. Brief\norchestrator-brief\n")
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.capture = self.root / "paseo.json"
        paseo = self.bin / "paseo"
        paseo.write_text("#!/usr/bin/env python3\nimport json, os, pathlib, sys\n"
                         "pathlib.Path(os.environ['CAPTURE']).write_text(json.dumps(sys.argv[1:]))\n"
                         "print('stub-worker')\nsys.exit(int(os.environ.get('PASEO_RC', '0')))\n")
        paseo.chmod(0o755)
        state = self.root / ".jira-followup"
        state.mkdir(parents=True)
        (state / ".running-fixture").write_text(json.dumps({"token": "fixture"}))
        os.replace(state / ".running-fixture", state / "running.json")
        self.env = dict(os.environ, HOME=str(self.root), JIRA_WAKE_DIR=str(state), PATH=f"{self.bin}:{os.environ['PATH']}",
                        CAPTURE=str(self.capture), JIRA_WORKER_CWD=str(self.root))

    def tearDown(self):
        self.tmp.cleanup()

    def worker(self, brief, *extra):
        path = self.root / "brief.md"
        path.write_text(brief)
        return subprocess.run(["/bin/bash", str(self.scripts / "jira-worker"),
                               "sol", "PROJ-1 unit", str(path), *extra, "--token", "fixture"],
                              env=self.env, capture_output=True, text=True, timeout=10)

    def prompt(self):
        return json.loads(self.capture.read_text())[-1]

    def test_authority_is_included_without_citations(self):
        result = self.worker("목표: 시험\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("authority-body", self.prompt())
        self.assertNotIn("ticket-body", self.prompt())
        self.assertNotIn("morning-1-body", self.prompt())

    def test_ticket_and_conflict_citations_append_only_those_sections(self):
        result = self.worker("목표: 충돌 해결\nRUNBOOK §7-3, §7-3-C, §7-3\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        prompt = self.prompt()
        for text in ("authority-body", "ticket-body", "conflict-body"):
            self.assertEqual(prompt.count(text), 1)
        self.assertNotIn("other-body", prompt)
        self.assertNotIn("morning-1-body", prompt)
        self.assertIn("목표: 충돌 해결", prompt)

    def test_morning_range_expands_all_six_sections(self):
        result = self.worker("RUNBOOK §1~§6\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        for number in range(1, 7):
            self.assertIn(f"morning-{number}-body", self.prompt())
        self.assertNotIn("ticket-body", self.prompt())

    def test_dashed_step_range_keeps_prefix(self):
        for citation in ("§7-3-4~5", "§7-3-1~§7-3-3"):
            with self.subTest(citation=citation):
                result = self.worker(f"RUNBOOK {citation}\n")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("ticket-body", self.prompt())
                self.assertNotIn("other-body", self.prompt())

    def test_explicit_orchestrator_reference_uses_that_document(self):
        result = self.worker("RUNBOOK §7-3; `ORCHESTRATOR.md` §2\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        prompt = self.prompt()
        self.assertIn("authority-body", prompt)
        self.assertIn("ticket-body", prompt)
        self.assertIn("orchestrator-assignment", prompt)
        self.assertNotIn("morning-2-body", prompt)
        self.assertNotIn("orchestrator-brief", prompt)

    def test_brief_paths_are_data_and_never_read(self):
        secret = self.root / "secret.md"
        secret.write_text("SHOULD-NOT-BE-READ")
        result = self.worker(f"RUNBOOK §7-3\n참고: {secret}\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("SHOULD-NOT-BE-READ", self.prompt())

    def test_missing_requested_section_prevents_launch(self):
        result = self.worker("RUNBOOK §7-99\n")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("7-99", result.stderr)
        self.assertFalse(self.capture.exists())

    def test_numbered_step_citations_include_their_parent_once(self):
        result = self.worker("RUNBOOK §7-3-0, §7-3-2, §7-3-6, §7-3\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.prompt().count("ticket-body"), 1)
        self.assertNotIn("conflict-body", self.prompt())

    def test_unknown_numbered_step_prevents_launch(self):
        result = self.worker("RUNBOOK §7-3-99\n")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("7-3-99", result.stderr)
        self.assertFalse(self.capture.exists())

    def test_actual_orchestrator_citations_resolve_against_runbook(self):
        skill = SCRIPTS.parent
        brief = (skill / "ORCHESTRATOR.md").read_text()
        prompt = SECTION.worker_sections(skill, brief)
        self.assertEqual(prompt.count("## 0. 모드"), 1)
        self.assertEqual(prompt.count("### 7-3. 작업 진행"), 1)
        self.assertEqual(prompt.count("### 7-3-C. 병합 충돌 해결"), 1)

    def test_missing_authority_prevents_launch(self):
        self.runbook.write_text("## 1. Only\nno-authority\n")
        result = self.worker("목표: 시험\n")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("0", result.stderr)
        self.assertFalse(self.capture.exists())

    def test_missing_orchestrator_section_prevents_launch(self):
        result = self.worker("ORCHESTRATOR §9\n")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("9", result.stderr)
        self.assertFalse(self.capture.exists())

    def test_missing_policy_file_prevents_launch(self):
        self.runbook.unlink()
        result = self.worker("목표: 시험\n")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("RUNBOOK.md", result.stderr)
        self.assertFalse(self.capture.exists())

    def test_invalid_range_prevents_launch(self):
        for citation in ("§6~§1", "§0~§999999999", "§7-3-5~4"):
            with self.subTest(citation=citation):
                result = self.worker(f"RUNBOOK {citation}\n")
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(self.capture.exists())

    def test_timeout_cwd_and_worker_failure_are_preserved(self):
        self.env["PASEO_RC"] = "3"
        result = self.worker("RUNBOOK §7-3\n", "120m")
        self.assertEqual(result.returncode, 3, result.stderr)
        argv = json.loads(self.capture.read_text())
        self.assertEqual(argv[argv.index("--cwd") + 1], str(self.root))
        self.assertEqual(argv[argv.index("--wait-timeout") + 1], "120m")


if __name__ == "__main__":
    unittest.main()
