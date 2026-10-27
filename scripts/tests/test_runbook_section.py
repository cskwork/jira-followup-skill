"""Offline checks for heading-based RUNBOOK section extraction."""
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / "runbook_section.py"
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


if __name__ == "__main__":
    unittest.main()
