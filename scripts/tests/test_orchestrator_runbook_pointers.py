"""Orchestrator docs point at RUNBOOK sections; keep every pointer resolvable."""
from pathlib import Path
import re
import subprocess
import sys
import unittest

SCRIPTS = Path(__file__).resolve().parents[1]
SKILL = SCRIPTS.parent
sys.path.insert(0, str(SCRIPTS))
import runbook_section  # noqa: E402

START_COMMAND = re.compile(r'runbook_section\.py "\$S/\.\./RUNBOOK\.md" ([0-9A-Za-z -]+)`')
ORCHESTRATOR_COMMAND = re.compile(r'runbook_section\.py "\$S/\.\./ORCHESTRATOR\.md" ([0-9A-Za-z -]+)`')


class OrchestratorRunbookPointers(unittest.TestCase):
    def test_every_ticket_orchestrator_citation_resolves(self):
        brief = (SKILL / "TICKET-ORCHESTRATOR.md").read_text(encoding="utf-8")
        prompt = runbook_section.worker_sections(SKILL, brief)
        self.assertIn("### 7-3. 작업 진행", prompt)
        self.assertIn("### 7-3-L. 자원 잠금", prompt)

    def test_start_section_command_resolves_with_cli(self):
        for name in ("TICKET-ORCHESTRATOR.md", "ORCHESTRATOR.md"):
            with self.subTest(document=name):
                text = (SKILL / name).read_text(encoding="utf-8")
                commands = START_COMMAND.findall(text)
                self.assertEqual(len(commands), 1, "one start command near the top")
                result = subprocess.run(
                    [sys.executable, str(SCRIPTS / "runbook_section.py"), str(SKILL / "RUNBOOK.md"),
                     *commands[0].split()],
                    capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("## 0. 모드", result.stdout)

    def test_ticket_orchestrator_reads_orchestrator_sections_not_whole_file(self):
        text = (SKILL / "TICKET-ORCHESTRATOR.md").read_text(encoding="utf-8")
        commands = ORCHESTRATOR_COMMAND.findall(text)
        self.assertEqual(len(commands), 1)
        result = subprocess.run(
            [sys.executable, str(SCRIPTS / "runbook_section.py"), str(SKILL / "ORCHESTRATOR.md"), *commands[0].split()],
            capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        for heading in ("### 2-A.", "## 3. brief", "## 4. 결과 확인"):
            self.assertIn(heading, result.stdout)
        self.assertIn("ticket_packet.py", text)

    def test_tick_core_stays_small(self):
        # 전역 오케스트레이터가 매 tick 읽는다(2026-11-16 맥락 줄이기 목표 15KB).
        self.assertLessEqual((SKILL / "ORCHESTRATOR.md").stat().st_size, 15 * 1024)


if __name__ == "__main__":
    unittest.main()
