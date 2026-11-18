"""Offline checks for heading-based RUNBOOK section extraction."""
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / "runbook_section.py"
sys.path.insert(0, str(SCRIPT.parent))
import runbook_section  # noqa: E402
DOCUMENT = """# Runbook
## 0. Authority
authority-only
### Details
authority-details
## 7. Work
work-intro
### 7-3. Ticket
ticket-only
0. **Step**: ticket-step
#### Notes
ticket-notes
```md
## 88. This is a code example
```
### 7-3-C. Conflict
conflict-only
### 7-4. Other
other-only
## Appendix
appendix-only
"""


class RunbookSectionChecks(unittest.TestCase):
    def extract(self, *sections):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "run book.md"
            source.write_text(DOCUMENT)
            return subprocess.run([sys.executable, str(SCRIPT), str(source), *sections],
                                  capture_output=True, text=True, timeout=10)

    def test_section_includes_deeper_headings_and_excludes_siblings(self):
        result = self.extract("0", "7-3")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("## 0. Authority\nauthority-only", result.stdout)
        self.assertIn("authority-details", result.stdout)
        self.assertIn("ticket-notes", result.stdout)
        self.assertIn("## 88. This is a code example", result.stdout)
        self.assertNotIn("work-intro", result.stdout)
        self.assertNotIn("conflict-only", result.stdout)
        self.assertNotIn("other-only", result.stdout)

    def test_letter_suffix_and_unnumbered_boundary(self):
        result = self.extract("7-3-C", "7-4")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("conflict-only", result.stdout)
        self.assertIn("other-only", result.stdout)
        self.assertNotIn("ticket-only", result.stdout)
        self.assertNotIn("appendix-only", result.stdout)

    def test_missing_section_fails_without_partial_output(self):
        result = self.extract("0", "7-99")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertIn("7-99", result.stderr)

    def test_repeated_section_is_printed_once(self):
        result = self.extract("0", "0")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.count("## 0. Authority"), 1)

    def test_parent_and_child_do_not_duplicate_text(self):
        result = self.extract("7-3", "7", "7-3-C")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.count("ticket-only"), 1)
        self.assertEqual(result.stdout.count("conflict-only"), 1)

    def test_cli_does_not_resolve_numbered_steps_as_headings(self):
        result = self.extract("7-3-0")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("missing section §7-3-0", result.stderr)

    def test_cli_usage_and_missing_file_fail(self):
        for args in ([], ["missing.md"], ["missing.md", "0"]):
            result = subprocess.run([sys.executable, str(SCRIPT), *args],
                                    capture_output=True, text=True, timeout=10)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(result.stdout, "")


class WorkerDigestChecks(unittest.TestCase):
    """작업자 프롬프트: RUNBOOK §0은 항상, 오케스트레이터 전용 §7-6은 인용돼도 빼고, ORCHESTRATOR는 참조 파일까지 찾는다."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.skill = Path(temporary.name)
        (self.skill / "RUNBOOK.md").write_text(
            "# R\n## 0. Authority\nauthority-only\n### 0-1. Records\nrecord-rules\n"
            "## 7. Work\n### 7-2. Intent\nintent-only\n### 7-6. Notify\nnotify-only\n### 7-7. Notes\nnotes-only\n")
        (self.skill / "ORCHESTRATOR.md").write_text(
            "# O\n## 0. Boundary\ncore-boundary\n## 2. Units\n### 2-A. Table\ncore-table\n## 5. Report\ncore-report\n")
        (self.skill / "ORCHESTRATOR-REFERENCE.md").write_text(
            "# Ref\n### 2-3. Branch\nref-branch\n## 3. Brief\nref-brief\n## Unnumbered\n### 5-1. Spec\nref-spec\n")

    def test_runbook_7_6_is_never_sent_to_workers(self):
        prompt = runbook_section.worker_sections(self.skill, "RUNBOOK §7-6, §7-2, §7-7\n")
        self.assertIn("record-rules", prompt)
        self.assertIn("intent-only", prompt)
        self.assertIn("notes-only", prompt)
        self.assertNotIn("notify-only", prompt)

    def test_orchestrator_citations_resolve_in_core_or_reference(self):
        prompt = runbook_section.worker_sections(self.skill, "ORCHESTRATOR §2-3, ORCHESTRATOR §5-1, ORCHESTRATOR §5\n")
        for text in ("ref-branch", "ref-spec", "core-report"):
            self.assertEqual(prompt.count(text), 1, text)
        self.assertNotIn("core-table", prompt)
        self.assertNotIn("ref-brief", prompt)

    def test_cli_reads_reference_with_orchestrator(self):
        result = subprocess.run([sys.executable, str(SCRIPT), str(self.skill / "ORCHESTRATOR.md"), "2-A", "3"],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("core-table", result.stdout)
        self.assertIn("ref-brief", result.stdout)
        self.assertNotIn("ref-spec", result.stdout)

    def test_section_number_in_both_files_is_a_duplicate(self):
        (self.skill / "ORCHESTRATOR-REFERENCE.md").write_text("# Ref\n## 5. Again\ndup\n")
        with self.assertRaisesRegex(ValueError, "duplicate section §5"):
            runbook_section.worker_sections(self.skill, "ORCHESTRATOR §5\n")


if __name__ == "__main__":
    unittest.main()
