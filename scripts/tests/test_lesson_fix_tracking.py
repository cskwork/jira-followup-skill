"""fix-now로 정한 반복 실패는 '[공장 수정]' 할 일로 남는다(2026-11-10 회고: 11-06 17:27 fix-now 두 건이 11-09에 재발했는데
아무것도 수정을 추적하지 않았다). to-do 명령은 가짜로 받아 적는다."""
import contextlib
from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import decision_inbox  # noqa: E402
import decision_page  # noqa: E402
import lessons  # noqa: E402
import state_update  # noqa: E402

WHAT = 'PROJ-1505 gate completion not-complete commentId=12345'


class FakeTodo:
    def __init__(self):
        self.calls, self.done = [], set()

    def __call__(self, args):
        self.calls.append(args)
        if args[0] == 'add':
            return 'fx1'
        if args[0] == 'list':
            return [{'id': 'fx1', 'title': '[공장 수정] x', 'status': 'done' if 'fx1' in self.done else 'waiting'}]
        if args[0] == 'done':
            self.done.add(args[1])
        return ''

    def named(self, command):
        return [c for c in self.calls if c[0] == command]


class FixTracking(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name).resolve()
        self.now = datetime(2026, 11, 6, 8, tzinfo=timezone.utc)
        self.todo = FakeTodo()
        self.opener = Mock(side_effect=self.open_decision)

    def open_decision(self, directory, path):
        spec, name = decision_page.load_spec('spec', str(path), directory)
        code = decision_page.register_code(directory, spec, name, None, server=False)
        decision_page.update_code(directory, code, createdAt=self.clock.isoformat())  # 시험 시계로 맞춘다.
        return code

    def add(self, now=None, **extra):
        now = self.clock = now or self.now
        return lessons.append(self.directory, dict(kind='infra_fail', what=WHAT, at=now.isoformat(),
                                                   evidence='runs/attempt/gate.json', **extra),
                              now=now, opener=self.opener, todo=self.todo)

    def codes(self):
        return json.loads((self.directory / 'decision-pages/codes.json').read_text())

    def decide(self, option, *, by='todo-app', days_ago=0):
        """결정 수신기·judge가 남기는 그대로: codes.json decided + decisions.jsonl 감사 줄."""
        codes = self.codes()
        code = max(codes, key=lambda c: int(c[1:]))
        message = f'todo:it:{code}' if by == 'todo-app' else f'judge:{code}'
        codes[code].update(state='decided', terminalAt=(self.now - timedelta(days=days_ago)).isoformat())
        if by == 'todo-app':
            codes[code]['messageId'] = message
        (self.directory / 'decision-pages/codes.json').write_text(json.dumps(codes))
        with (self.directory / 'decisions.jsonl').open('a') as f:
            f.write(json.dumps({'option': option, 'messageId': message, 'kind': 'choice'}) + '\n')
        return code

    def opened(self, days_ago=8):
        for _ in range(3):
            self.add(self.now - timedelta(days=days_ago))
        self.assertEqual(self.opener.call_count, 1)

    def test_fix_now_answer_creates_one_tracked_item_with_signature_count_dates_evidence(self):
        self.opened()
        code = self.decide('fix-now', days_ago=8)
        tracked = lessons.track_fixes(self.directory, todo=self.todo)
        sig = lessons.signature({'kind': 'infra_fail', 'what': WHAT})
        self.assertEqual(tracked, {sig: 'fx1'})
        [add] = self.todo.named('add')
        self.assertTrue(add[1].startswith('[공장 수정] PROJ-1505 gate completion not-complete'))
        note = ' '.join(c[2] for c in self.todo.named('note'))
        for part in (sig, '3회', '2026-10-29', 'runs/attempt/gate.json'):
            self.assertIn(part, note)
        self.assertIn(code, self.todo.named('wait')[0][2])
        # 같은 결정은 두 번 만들지 않는다.
        lessons.track_fixes(self.directory, todo=self.todo)
        self.assertEqual(len(self.todo.named('add')), 1)

    def test_judge_fix_now_is_tracked_and_accept_ignore_are_not(self):
        self.opened()
        self.decide('fix-now', by='judge', days_ago=8)
        self.assertEqual(len(lessons.track_fixes(self.directory, todo=self.todo)), 1)
        for option in ('accept', 'ignore'):
            with self.subTest(option=option):
                todo = FakeTodo()
                with tempfile.TemporaryDirectory() as other:
                    self.directory = Path(other).resolve()
                    self.opener.reset_mock()
                    self.opened(days_ago=1)
                    self.decide(option, days_ago=1)
                    self.assertEqual(lessons.track_fixes(self.directory, todo=todo), {})
                    self.assertFalse(todo.named('add'))
                    # 오늘의 동작 그대로: 7일 동안 다시 열지 않는다.
                    self.assertEqual(self.add()['decision'], 'answer-cooldown')

    def test_open_request_outranks_recent_answer_in_either_code_order(self):
        # 같은 lesson에 최근 답한 코드와 뒤에 열린 코드가 함께 있으면 codes 순서와 무관하게 already-open이다.
        self.opened(days_ago=1)
        decided = self.decide('accept', days_ago=1)
        codes = self.codes()
        open_code = dict(codes[decided], state='open')
        open_code.pop('terminalAt')
        for order in ('decided-first', 'open-first'):
            with self.subTest(order=order):
                pairs = [(decided, codes[decided]), ('D999', open_code)]
                if order == 'open-first':
                    pairs.reverse()
                (self.directory / 'decision-pages/codes.json').write_text(json.dumps(dict(pairs)))
                self.assertEqual(self.add()['decision'], 'already-open')
        self.assertEqual(self.opener.call_count, 1)

    def test_recurrence_while_open_adds_one_line_per_day_instead_of_reopening(self):
        self.opened()
        self.decide('fix-now', days_ago=8)  # 7일 냉각이 끝났어도 결정을 다시 열지 않는다.
        self.assertEqual(self.add()['decision'], 'fix-tracked')
        self.assertEqual(self.add()['decision'], 'fix-tracked')
        self.assertEqual(self.opener.call_count, 1)
        notes = [c[2] for c in self.todo.named('note') if c[2].startswith('fix-now 뒤')]
        self.assertEqual(notes, ['fix-now 뒤 재발 1회, 최근 2026-11-06'])
        self.add(self.now + timedelta(days=1))
        notes = [c[2] for c in self.todo.named('note') if c[2].startswith('fix-now 뒤')]
        self.assertEqual(notes[-1], 'fix-now 뒤 재발 3회, 최근 2026-11-07')
        self.assertEqual(len(notes), 2)

    def test_already_decided_signature_is_tracked_at_first_recurrence(self):
        # 2026-11-06 D67·D68처럼 이 기능 전에 fix-now로 정한 서명도 다음 재발에서 항목이 생긴다.
        self.opened()
        self.decide('fix-now', days_ago=8)
        self.assertEqual(self.add()['decision'], 'fix-tracked')
        self.assertEqual(len(self.todo.named('add')), 1)

    def test_lesson_fixed_closes_item_and_next_recurrence_reopens_fix_failed_decision(self):
        self.opened()
        self.decide('fix-now', days_ago=8)
        sig = next(iter(lessons.track_fixes(self.directory, todo=self.todo)))
        result = lessons.mark_fixed(self.directory, sig[:12], 'abc1234', now=self.now, todo=self.todo)
        self.assertEqual(result, {'signature': sig, 'state': 'fixed', 'commit': 'abc1234'})
        self.assertEqual(self.todo.named('done'), [['done', 'fx1', '수정 커밋 abc1234로 닫음']])
        later = self.now + timedelta(hours=1)
        self.assertEqual(self.add(later)['decision'], 'opened-fix-failed')
        self.assertEqual(self.opener.call_count, 2)
        spec = json.loads(self.opener.call_args.args[1].read_text())
        self.assertIn('다시 났어요', spec['title'])
        self.assertIn('abc1234', spec['summary'])
        self.assertEqual(spec['recommended'], 'fix-now')
        # 열린 결정이 있으면 더 열지 않는다.
        self.assertEqual(self.add(later)['decision'], 'already-open')
        with self.assertRaisesRegex(ValueError, 'expected one open'):
            lessons.mark_fixed(self.directory, sig[:12], 'abc1234', todo=self.todo)

    def test_new_fix_now_after_fix_failed_tracks_a_new_item(self):
        self.opened()
        self.decide('fix-now', days_ago=8)
        sig = next(iter(lessons.track_fixes(self.directory, todo=self.todo)))
        lessons.mark_fixed(self.directory, sig, 'abc1234', now=self.now - timedelta(days=1), todo=self.todo)
        self.add()
        self.decide('fix-now')
        self.assertEqual(lessons.track_fixes(self.directory, todo=self.todo), {sig: 'fx1'})
        self.assertEqual(len(self.todo.named('add')), 2)

    def test_human_closed_item_counts_as_closed(self):
        self.opened()
        self.decide('fix-now', days_ago=8)
        lessons.track_fixes(self.directory, todo=self.todo)
        self.todo.done.add('fx1')
        self.assertEqual(self.add()['decision'], 'opened-fix-failed')
        spec = json.loads(self.opener.call_args.args[1].read_text())
        self.assertIn('사람이 항목을 닫음', spec['summary'])

    def test_cli_lesson_fixed_records_commit_and_rejects_bad_input(self):
        self.opened()
        self.decide('fix-now', days_ago=8)
        sig = next(iter(lessons.track_fixes(self.directory, todo=self.todo)))
        stderr = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(stderr):
            self.assertEqual(state_update.main(['--dir', str(self.directory), 'lesson-fixed', sig[:12],
                                                '--commit', 'not-a-sha']), 1)
            self.assertEqual(state_update.main(['--dir', str(self.directory), 'lesson-fixed', sig[:12],
                                                '--commit', 'abc1234']), 0)
        self.assertEqual(json.loads(out.getvalue())['state'], 'fixed')
        fixes = json.loads((self.directory / lessons.FIXES).read_text())
        self.assertEqual((fixes[sig]['state'], fixes[sig]['commit']), ('fixed', 'abc1234'))

    def test_receiver_poll_tracks_fix_now_decisions(self):
        self.opened()
        self.decide('fix-now', days_ago=8)
        with patch.dict(os.environ, JIRA_DECISION_SURFACE='app'):
            decision_inbox.poll(self.directory, todo=self.todo, item_cli=Mock())
        self.assertEqual(len([c for c in self.todo.named('add') if c[1].startswith('[공장 수정] ')]), 1)


if __name__ == '__main__':
    unittest.main()
