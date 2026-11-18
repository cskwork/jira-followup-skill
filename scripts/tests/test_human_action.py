"""사람만 할 수 있는 선행 조치는 질문이 아니라 할 일이다(2026-11-10 회고: PROJ-1434 서버 공유 키를 '넣은 뒤 알려 주실 수
있나요?'로 묻고 권장 답을 채워, 11-03~06 한 단어 답 9회가 아무것도 바꾸지 못했고 '직접 해줘' 거절은 큐 note에만 남았다).
to-do 명령·판정 모델은 가짜로 받아 적는다."""
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import decision_inbox as inbox  # noqa: E402
import followup_scan as scan  # noqa: E402
import judge  # noqa: E402
import state_update as state  # noqa: E402

KEY = 'PROJ-1434'
ACTION = '개발계 세 서비스에 서버 공유 키 시크릿을 만들어 넣기'
SHEET = '.sdlc/work/proj-1434-otsd-server-auth/logs/secret-runsheet.md'
QUESTION = '실행지대로 개발계 서비스들에 서버 공유 키를 넣은 뒤 알려 주실 수 있나요?'
SUGGEST = '실행지대로 개발계 시크릿 넣었음, 시크릿 준비 완료'
SHOWN = f'공장 대기 — 할 일: {ACTION} · 실행지: {SHEET}{scan.ACTION_TAIL}'


def row(**changes):
    base = dict(id='de78e7', title=f'{KEY} 서버 인증', status='open', waiting='', asked='', unread=0,
                replies=[], suggest='', link={'label': KEY, 'url': 'u'})
    return dict(base, **changes)


def run(directory, *args):
    stderr = io.StringIO()
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(stderr):
        try:
            return state.main(['--dir', str(directory), 'patch', 'followup-queue.json', '--path', json.dumps([KEY]),
                               *args]), stderr.getvalue()
        except SystemExit as error:
            return error.code, stderr.getvalue()


class PatchAction(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        state.atomic_json(self.directory / 'followup-queue.json', {KEY: {
            'waiting': 'human-risk', 'note': '기록', 'question': QUESTION, 'suggest': SUGGEST}})

    def queue(self):
        return state.read_object(self.directory / 'followup-queue.json')[KEY]

    def test_action_replaces_question_and_suggest_and_needs_no_suggest(self):
        code, _ = run(self.directory, '--json', '{"waiting": "human-risk"}', '--action', ACTION, '--run-sheet', SHEET)
        self.assertEqual(code, 0)
        item = self.queue()
        self.assertEqual((item['action'], item['runSheet']), (ACTION, SHEET))
        self.assertNotIn('question', item)
        self.assertNotIn('suggest', item)
        # 질문 형식으로 돌아가면 할 일은 지운다. 대기가 풀리면 모두 지운다.
        state.patch(self.directory, 'followup-queue.json', [KEY], {'question': QUESTION, 'suggest': SUGGEST})
        self.assertNotIn('action', self.queue())
        state.patch(self.directory, 'followup-queue.json', [KEY], {'action': ACTION})
        state.patch(self.directory, 'followup-queue.json', [KEY], {'waiting': None})
        self.assertNotIn('action', self.queue())

    def test_action_is_exclusive_with_suggest(self):
        code, stderr = run(self.directory, '--json', '{"waiting": "human-risk"}', '--action', ACTION, '--suggest', SUGGEST)
        self.assertEqual(code, 2)
        self.assertIn('not allowed with', stderr)
        code, stderr = run(self.directory, '--json', json.dumps({'action': ACTION, 'suggest': SUGGEST}))
        self.assertEqual(code, 1)
        self.assertIn('excludes', stderr)
        code, stderr = run(self.directory, '--json', '{}', '--run-sheet', SHEET)
        self.assertEqual(code, 1)
        self.assertIn('--run-sheet needs --action', stderr)
        self.assertEqual(self.queue()['suggest'], SUGGEST)


class TodoShowsAction(unittest.TestCase):
    queue = {KEY: {'waiting': 'human-risk', 'note': '기록', 'action': ACTION, 'runSheet': SHEET}}

    def test_action_wait_shows_todo_and_clears_suggest(self):
        actions = scan.todo_actions([(row(suggest=SUGGEST), KEY)], self.queue)
        self.assertEqual(actions, [['wait', 'de78e7', SHOWN], ['suggest', 'de78e7', '']])
        self.assertEqual(scan.todo_actions([(row(waiting=SHOWN), KEY)], self.queue), [])

    def test_refused_reply_gets_one_line_with_ask_rule_and_remaining_action(self):
        reply = {'at': '2026-11-06 14:40', 'who': 'human', 'what': 'reply — 시크릿 준비 직접해줘'}
        answered = row(asked=SHOWN, unread=1, replies=[reply])
        handled = {scan.todo_reply_id(answered, reply): {
            'key': KEY, 'action': 'refused', 'rule': 'RUNBOOK §0: 비밀값 생성·주입은 사람만'}}
        refused = scan.refusals([answered], handled)
        told = (f"공장 대기 — 받은 답 '시크릿 준비 직접해줘'은(는) 공장이 할 수 없어요 "
                f"[RUNBOOK §0: 비밀값 생성·주입은 사람만] — 할 일: {ACTION} · 실행지: {SHEET}{scan.ACTION_TAIL}")
        self.assertEqual(scan.todo_actions([(answered, KEY)], self.queue, refused=refused), [['wait', 'de78e7', told]])
        # 같은 거절 답에는 한 번만 쓴다.
        settled = row(waiting=told, asked=told, replies=[reply])
        self.assertEqual(scan.todo_actions([(settled, KEY)], self.queue, refused=scan.refusals([settled], handled)), [])
        # 다른 처리(거절 아님)는 지금처럼 '바뀐 것이 없어요'다.
        self.assertEqual(scan.refusals([answered], {scan.todo_reply_id(answered, reply): {'action': 'human'}}), {})

    def test_alert_shows_action_without_suggest(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            (directory / 'followup-queue.json').write_text(json.dumps(self.queue, ensure_ascii=False))
            calls = []

            def todo(args):
                calls.append(args)
                return [row()] if args[0] == 'list' else None
            with patch.dict(os.environ, JIRA_DECISION_SURFACE='app'), patch.object(inbox, 'ticket_phase', return_value=''):
                inbox.alert('human-risk: 시크릿 필요', directory, key=KEY, todo=todo)
            self.assertIn(['wait', 'de78e7', SHOWN], calls)
            self.assertFalse([c for c in calls if c[0] in ('suggest', 'choices')])


class JudgeSkipsAction(unittest.TestCase):
    def test_risk_never_asks_model_about_human_action(self):
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ):
            directory = Path(temporary)
            os.environ.pop('JIRA_JUDGE_MODE', None)
            (directory / 'judge').write_text('on\n')
            # 옛 항목처럼 질문·권장 답이 남아 있어도 할 일 대기는 판정하지 않는다.
            state.atomic_json(directory / 'followup-queue.json', {KEY: {
                'waiting': 'human-risk', 'stage': 'tobe', 'action': ACTION, 'question': QUESTION, 'suggest': SUGGEST}})
            self.assertEqual(judge.risk(directory, model=lambda data: self.fail('judge asked about a human action')), 0)


if __name__ == '__main__':
    unittest.main()
