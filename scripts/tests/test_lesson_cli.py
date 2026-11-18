"""Public lesson command regression; S3_SCRIPTS can select the original checkout."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPTS = Path(os.environ.get('S3_SCRIPTS', str(Path(__file__).resolve().parents[1])))


class LessonCLI(unittest.TestCase):
    def test_cli_records_lesson_and_reports_no_decision_before_threshold(self):
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run([sys.executable, str(SCRIPTS / 'state_update.py'),
                '--dir', directory, 'lesson', '--json', json.dumps({
                    'class': 'other', 'kind': 'infra_fail', 'what': 'PROJ-1505 gate completion not-complete',
                    'evidence': ['gate.json']})], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)['count'], 1)
            self.assertEqual(json.loads(result.stdout)['decision'], 'not-needed')
            row = json.loads((Path(directory) / 'lessons.jsonl').read_text())
            self.assertEqual(row['kind'], 'infra_fail')
            self.assertIn('signature', row)
            self.assertIn('at', row)
