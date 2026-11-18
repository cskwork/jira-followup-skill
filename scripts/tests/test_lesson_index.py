"""닫힌 분류, 과거 서명 호환과 읽기 전용 lesson index의 계약."""
import contextlib
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock

import _harness  # noqa: F401 -- 단독 실행도 실제 외부 연동을 막는다.

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
import lessons  # noqa: E402
import state_update  # noqa: E402

LEGACY = '26af22b365c6db0e8def13eb43f89f8bdb254d8074d8b85993ae9acbf5fb3cbd'
WHAT = 'PROJ-1505 gate completion not-complete commentId=12345'


class LessonIndex(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.now = datetime(2026, 11, 16, 12, tzinfo=timezone.utc)

    def row(self, **values):
        return dict({'kind': 'infra_fail', 'what': WHAT, 'at': self.now.isoformat(),
                     'suggestion': '다음 실행 전에 근거를 확인한다.'}, **values)

    def write(self, rows):
        (self.directory / 'lessons.jsonl').write_text(
            ''.join(json.dumps(row, ensure_ascii=False) + '\n' for row in rows), encoding='utf-8')

    def fixes(self, values):
        path = self.directory / lessons.FIXES
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(values), encoding='utf-8')

    def test_closed_registry_and_strict_cli_reject_missing_and_unknown_without_writes(self):
        registry = lessons.classes()
        self.assertTrue(12 <= len(registry) <= 15)
        self.assertIn('other', registry)
        self.assertTrue(all(isinstance(value, str) and value.strip() and '\n' not in value
                            for value in registry.values()))
        for value in (None, 'invented', 42, ''):
            row = self.row()
            if value is not None:
                row['class'] = value
            with self.subTest(value=value), contextlib.redirect_stderr(io.StringIO()) as output:
                code = state_update.main(['lesson', '--dir', str(self.directory), '--json', json.dumps(row)])
                self.assertNotEqual(code, 0)
                self.assertIn('lesson class must be one of:', output.getvalue())
                self.assertTrue(all(name in output.getvalue() for name in registry))
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_append_accepts_classless_code_writers_but_rejects_unknown_class(self):
        result = lessons.append(self.directory, self.row(), now=self.now, opener=Mock(), todo=lambda _: [])
        self.assertEqual(result['signature'], LEGACY)
        before = (self.directory / 'lessons.jsonl').read_bytes()
        with self.assertRaisesRegex(ValueError, 'lesson class must be one of:'):
            lessons.append(self.directory, self.row(**{'class': 'invented'}), now=self.now)
        self.assertEqual((self.directory / 'lessons.jsonl').read_bytes(), before)

    def test_legacy_and_other_signatures_are_literal_byte_identical(self):
        for row in (self.row(), self.row(**{'class': 'other'}), self.row(kind=' INFRA_FAIL ',
                what='PROJ-42 gate completion not-complete comment_id=987 2026-10-11T09:10:00Z abcdef12')):
            self.assertEqual(lessons.signature(row), LEGACY)
        self.assertEqual(lessons.signature({'kind': 'guard', 'what': 'lease vanished 42'}),
                         'a2e1193ace4f97ea0f6f4dab4664ffdfb0dd587a9b6701fb36280daf8916f5c1')

    def test_class_signature_ignores_what_but_keeps_normalized_kind(self):
        first = self.row(kind=' INFRA_FAIL ', **{'class': 'requirements'})
        expected = hashlib.sha256(b'infra_fail:class:requirements').hexdigest()
        self.assertEqual(lessons.signature(first), expected)
        self.assertEqual(lessons.signature(self.row(what='entirely different', **{'class': 'requirements'})), expected)
        self.assertNotEqual(lessons.signature(self.row(kind='guard', **{'class': 'requirements'})), expected)
        opener = Mock()
        for what in ('alpha', 'beta', 'gamma'):
            result = lessons.append(self.directory, self.row(what=what, **{'class': 'requirements'}),
                                    now=self.now, opener=opener, todo=lambda _: [])
        self.assertEqual(result['count'], 3)
        self.assertEqual(opener.call_count, 1)

    def test_existing_legacy_open_decision_lookup_survives_other_class(self):
        self.write([self.row(), self.row()])
        codes = self.directory / 'decision-pages/codes.json'
        codes.parent.mkdir()
        codes.write_text(json.dumps({'D1': {'name': 'lesson-' + LEGACY, 'state': 'open'}}))
        opener = Mock()
        result = lessons.append(self.directory, self.row(**{'class': 'other'}), now=self.now,
                                opener=opener, todo=lambda _: [])
        self.assertEqual(result, {'signature': LEGACY, 'count': 3, 'decision': 'already-open'})
        opener.assert_not_called()

    def test_existing_legacy_fix_lookup_survives_other_class(self):
        self.fixes({LEGACY: {'state': 'open', 'decidedAt': self.now.isoformat(),
                             'notedDay': self.now.astimezone(lessons.TZ).date().isoformat()}})
        result = lessons.append(self.directory, self.row(**{'class': 'other'}), now=self.now,
                                opener=Mock(), todo=lambda _: [])
        self.assertEqual(result['signature'], LEGACY)
        self.assertEqual(result['decision'], 'fix-tracked')

    def test_index_window_exact_filters_latest_suggestion_and_other_group(self):
        oldest = self.row(at=(self.now - timedelta(days=14)).isoformat(), stage='check',
                          suggestion='예전 제안', **{'class': 'requirements'})
        newest = self.row(stage='check', suggestion='최신\n 제안', **{'class': 'requirements'})
        self.write([newest, oldest, self.row(stage='build', **{'class': 'requirements'}),
                    self.row(kind='guard', stage='check', **{'class': 'requirements'}),
                    self.row(stage='check', at=(self.now - timedelta(days=14, seconds=1)).isoformat(), **{'class': 'scope'}),
                    self.row(stage='check', at=(self.now + timedelta(seconds=1)).isoformat(), **{'class': 'scope'}),
                    self.row(stage='check', what='one', **{'class': 'other'}),
                    self.row(stage='check', what='two', **{'class': 'other'})])
        result = lessons.index(self.directory, kind='infra_fail', stage='check', now=self.now)
        self.assertEqual(result.splitlines(), [
            '- other ×2 (last 11-16) — 다음 실행 전에 근거를 확인한다. [fix: none]',
            '- requirements ×2 (last 11-16) — 최신 제안 [fix: none]'])
        self.assertEqual(lessons.index(self.directory, stage='CHECK', now=self.now), '')

    def test_classless_clusters_require_two_and_use_short_what(self):
        self.write([self.row(), self.row(what='PROJ-4 gate completion not-complete commentId=88'),
                    self.row(what='unique legacy'), self.row(what='single classified', **{'class': 'scope'})])
        result = lessons.index(self.directory, now=self.now)
        self.assertEqual(len(result.splitlines()), 2)
        self.assertIn('×2', result.splitlines()[0])
        self.assertIn('- scope ×1', result.splitlines()[1])
        self.assertNotIn('unique legacy', result)

    def test_incomplete_historical_rows_are_skipped_like_append_history(self):
        incomplete = {'kind': 'infra_fail', 'at': (self.now - timedelta(days=15)).isoformat(),
                      'reason': 'old writer omitted what'}
        self.write([incomplete, dict(incomplete, at=self.now.isoformat()), self.row(**{'class': 'brief'})])
        self.assertIn('- brief ×1', lessons.index(self.directory, now=self.now))

    def test_index_merges_class_across_kinds_and_open_fix_precedes_fixed(self):
        first = self.row(**{'class': 'deploy'})
        second = self.row(kind='guard', **{'class': 'deploy'})
        self.write([first, second])
        self.fixes({lessons.signature(first): {'state': 'fixed', 'commit': 'abcdef1', 'closedAt': self.now.isoformat()},
                    lessons.signature(second): {'state': 'open'}})
        self.assertIn('- deploy ×2', lessons.index(self.directory, now=self.now))
        self.assertIn('[fix: open]', lessons.index(self.directory, now=self.now))
        self.fixes({lessons.signature(first): {'state': 'fixed', 'commit': 'abcdef1'}})
        self.assertIn('[fix: fixed abcdef1]', lessons.index(self.directory, now=self.now))

    def test_index_is_bounded_and_deterministic_without_any_state_writes(self):
        rows = [self.row(**{'class': name}, suggestion='긴 제안 ' * 200) for name in lessons.classes()]
        for word in ('alpha', 'bravo', 'charlie', 'delta', 'echo', 'foxtrot', 'golf', 'hotel'):
            rows.extend([self.row(what=word, suggestion='제안 ' * 200)] * 2)
        self.write(rows)
        self.fixes({lessons.signature(rows[0]): {'state': 'open'}})
        before = {path.relative_to(self.directory): (path.read_bytes(), path.stat().st_mtime_ns)
                  for path in self.directory.rglob('*') if path.is_file()}
        result = lessons.index(self.directory, max=999, now=self.now)
        self.assertEqual(len(result.splitlines()), 20)
        self.assertLessEqual(len(result) + 1, 2500)  # CLI의 마지막 줄바꿈 포함
        self.assertEqual(len(lessons.index(self.directory, max=2, now=self.now).splitlines()), 2)
        self.assertEqual(lessons.index(self.directory, max=0, now=self.now), '')
        after = {path.relative_to(self.directory): (path.read_bytes(), path.stat().st_mtime_ns)
                 for path in self.directory.rglob('*') if path.is_file()}
        self.assertEqual(before, after)
        self.write(list(reversed(rows)))
        self.assertEqual(lessons.index(self.directory, max=999, now=self.now), result)
        absent = self.directory / 'absent'
        self.assertEqual(lessons.index(absent, now=self.now), '')
        self.assertFalse(absent.exists())

    def test_cli_outputs_only_index_and_errors_have_no_partial_stdout(self):
        now = datetime.now(timezone.utc)
        self.write([self.row(at=now.isoformat(), stage='verify', **{'class': 'check-result'})])
        command = [sys.executable, str(SCRIPTS / 'lessons.py'), 'index', '--dir', str(self.directory)]
        result = subprocess.run([*command, '--stage', 'verify', '--max', '1'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(result.stdout.startswith('- check-result ×1'))
        self.assertEqual(len(result.stdout.splitlines()), 1)
        self.assertEqual(result.stderr, '')
        (self.directory / 'lessons.jsonl').write_text('{broken JSON\n')
        result = subprocess.run(command, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, '')
        self.assertIn('invalid lesson JSON', result.stderr)


if __name__ == '__main__':
    unittest.main()
