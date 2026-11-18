"""Recurrences use the existing decision registry and offline decision opener."""
from datetime import datetime, timedelta, timezone
import importlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class LessonTests(unittest.TestCase):
    def setUp(self):
        self.lessons = importlib.import_module('lessons')
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)
        self.now = datetime(2026, 11, 6, tzinfo=timezone.utc)
        self.opener = Mock(side_effect=self.open_decision)

    def open_decision(self, directory, path):
        import decision_page
        spec, name = decision_page.load_spec('spec', str(path), directory)
        return decision_page.register_code(directory, spec, name, None, server=False)

    def add(self, days=0, what='PROJ-1505 gate completion not-complete commentId=12345', **extra):
        return self.lessons.append(self.directory, dict(kind='infra_fail', what=what,
            at=(self.now - timedelta(days=days)).isoformat(), evidence='runs/attempt/gate.json', **extra),
            now=self.now, opener=self.opener)

    def codes(self):
        path = self.directory / 'decision-pages/codes.json'
        return json.loads(path.read_text()) if path.exists() else {}

    def test_signature_strips_ticket_ids_shas_numbers_times_and_comment_ids(self):
        first = {'kind': 'infra_fail', 'what': 'PROJ-1505 gate exit 4 2026-11-05T09:25:00+09:00 SHA abcdef123 commentId=12345'}
        second = {'kind': 'infra_fail', 'what': 'PROJ-1501 gate exit 7 2026-10-30T16:22:00+09:00 SHA 987fedab1 commentId=99999'}
        self.assertEqual(self.lessons.signature(first), self.lessons.signature(second))
        self.assertNotEqual(self.lessons.signature(first), self.lessons.signature(dict(second, kind='guard')))
        self.assertNotEqual(self.lessons.signature(first), self.lessons.signature(dict(second, what='different failure')))

    def test_third_occurrence_opens_recommended_decision_with_evidence_count_and_dates(self):
        self.add(days=13)
        self.add(days=1, what='PROJ-1501 gate completion not-complete commentId=55555')
        self.assertFalse(self.codes())
        result = self.add()
        self.assertEqual(result['count'], 3)
        self.assertEqual(self.opener.call_count, 1)
        spec = json.loads(self.opener.call_args.args[1].read_text())
        self.assertEqual(spec['recommended'], 'fix-now')
        self.assertEqual([o['id'] for o in spec['options']], ['fix-now', 'accept', 'ignore'])
        self.assertIn('3', spec['summary'])
        self.assertIn('2026-10-24', spec['summary'])
        self.assertIn('2026-11-06', spec['summary'])
        self.assertTrue(1 <= len(spec['evidence']) <= 3)
        self.assertEqual(list(self.codes().values())[0]['state'], 'open')

    def test_open_decision_deduplicates_further_occurrences(self):
        for _ in range(5):
            self.add()
        self.assertEqual(self.opener.call_count, 1)
        self.assertEqual(len((self.directory / 'lessons.jsonl').read_text().splitlines()), 5)

    def test_concurrent_writers_open_exactly_one_decision(self):
        self.add()
        self.add()
        with ThreadPoolExecutor(max_workers=3) as workers:
            results = list(workers.map(lambda _: self.add(), range(3)))
        self.assertEqual(self.opener.call_count, 1)
        self.assertEqual(sum(r['decision'] == 'opened' for r in results), 1)
        self.assertEqual(len(self.codes()), 1)

    def test_cli_uses_same_escalation_path(self):
        import contextlib
        import io
        import state_update
        from unittest.mock import patch
        self.add()
        self.add()
        with patch.object(self.lessons, 'open_request', side_effect=self.open_decision), contextlib.redirect_stdout(io.StringIO()) as output:
            code = state_update.main(['--dir', str(self.directory), 'lesson', '--json', json.dumps({
                'class': 'other', 'kind': 'infra_fail', 'what': 'PROJ-1505 gate completion not-complete commentId=9'})])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())['decision'], 'opened')

    def test_two_or_old_occurrences_do_not_escalate(self):
        self.add(days=15)
        self.add(days=14, what='PROJ-1501 gate completion not-complete commentId=22222')
        self.add(days=0)
        self.assertEqual(self.opener.call_count, 0)

    def test_answer_suppresses_reopening_for_seven_days(self):
        for _ in range(3):
            self.add()
        path = self.directory / 'decision-pages/codes.json'
        codes = self.codes()
        codes['D1'].update(state='decided', terminalAt=(self.now - timedelta(days=6)).isoformat())
        path.write_text(json.dumps(codes))
        self.add()
        self.assertEqual(self.opener.call_count, 1)
        codes['D1']['terminalAt'] = (self.now - timedelta(days=7)).isoformat()
        path.write_text(json.dumps(codes))
        self.add()
        self.assertEqual(self.opener.call_count, 2)

    def test_existing_legacy_rows_are_counted_without_rewriting(self):
        path = self.directory / 'lessons.jsonl'
        row = {'at': self.now.isoformat(), 'kind': 'infra_fail', 'what': 'PROJ-1501 gate completion not-complete commentId=8'}
        original = json.dumps(row) + '\n'
        path.write_text(original * 2)
        self.add()
        self.assertEqual(self.opener.call_count, 1)
        self.assertTrue(path.read_text().startswith(original * 2))

    def test_open_failure_is_explicit_and_keeps_appended_lesson(self):
        self.add()
        self.add()
        self.opener.side_effect = OSError('decision surface unavailable')
        with self.assertRaisesRegex(OSError, 'surface unavailable'):
            self.add()
        self.assertEqual(len((self.directory / 'lessons.jsonl').read_text().splitlines()), 3)

    def test_invalid_lesson_does_not_append(self):
        with self.assertRaises(ValueError):
            self.lessons.append(self.directory, {'what': 'missing kind'}, opener=self.opener)
        self.assertFalse((self.directory / 'lessons.jsonl').exists())
