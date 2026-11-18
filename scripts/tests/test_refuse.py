"""권한 밖 답의 거절 기록(state_update.py refuse)과 작업자 humanAction.

2026-11-06 PROJ-1434: '서버 공유 키를 넣은 뒤 알려 주실 수 있나요?'에 '시크릿 준비 직접해줘'라고 답했다. 공장 권한(RUNBOOK §0)
밖이라 거절했지만 거절을 쓰는 명령이 없어 질문·권장 답이 그대로 남았고, 11-09 judge가 같은 질문을 다시 사람에게 올렸다.
기록 모양은 상태 폴더(큐·handled·judge-state)의 실제 PROJ-1434 줄을 줄인 것이다. to-do·판정 모델은 가짜로 받아 적는다."""
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
import worker_runtime  # noqa: E402

KEY = 'PROJ-1434'
SLUG = 'proj-1434-otsd-server-auth'
QUESTION = '실행지대로 개발계 서비스들에 서버 공유 키를 넣은 뒤 알려 주실 수 있나요?'
SUGGEST = '실행지대로 개발계 시크릿 넣었음, 시크릿 준비 완료'
RULE = 'RUNBOOK §0 비밀값 생성·주입은 사람만'
ACTION = '개발계 세 서비스와 배치 서버에 서버 공유 키를 넣고 할 일에 알려 주기'
SHEET = '.sdlc/work/proj-1434-otsd-server-auth/logs/secret-runsheet.md'
REFUSED = {'at': '2026-11-06 14:40', 'who': 'human', 'what': 'reply — 시크릿 준비 직접해줘'}
DONE = {'at': '2026-11-10 18:20', 'who': 'human', 'what': 'reply — 실행지대로 시크릿 넣었음'}


def queue_item():
    return {'slug': SLUG, 'stage': 'tobe', 'waiting': 'human-risk', 'branch': 'fix/PROJ-1434',
            'commentIds': ['intake:PROJ-1434'], 'deferCount': 0, 'createdAt': '2026-10-29T11:57:49+09:00',
            'nextStep': 'dev-deploy: 환경별 시크릿 준비 확인 뒤 호출자 먼저 dev 배포',
            'note': '[2026-11-06T09:30:51+09:00 event todo:de78e7:382c902578] 키 등록은 사용자가 직접 요청',
            'question': QUESTION, 'suggest': SUGGEST}


def todo_row(**changes):
    base = dict(id='de78e7', title=f'{KEY} 외부 연동 주소 서버 인증', status='waiting', slug=SLUG, waiting='',
                asked=f'공장 대기 — {QUESTION}', unread=0, replies=[], suggest='', link={'label': KEY, 'url': 'u'})
    return dict(base, **changes)


def quiet(call, *args, **kwargs):
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        try:
            return call(*args, **kwargs)
        except SystemExit as error:
            return error.code


class Base(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.dir = Path(temporary.name)
        state.atomic_json(self.dir / 'followup-queue.json', {KEY: queue_item()})
        state.atomic_json(self.dir / 'state.json', {'followup': {'handled': {'todo:de78e7:382c902578': {
            'key': KEY, 'action': 'human', 'at': '2026-11-06T09:30:51+09:00', 'slug': SLUG}}}})
        self.reply = scan.todo_reply_id(todo_row(), REFUSED)

    def read(self, name):
        return state.read_object(self.dir / name)

    def refuse(self, *extra):
        return quiet(state.main, ['--dir', str(self.dir), 'refuse', KEY, '--reply', self.reply,
                                  '--rule', RULE, '--action', ACTION, '--run-sheet', SHEET, *extra])

    def scan(self, rows):
        calls = []

        def todo(args):
            calls.append(args)
            return rows if args[:1] == ['list'] else None
        quiet(scan.main, [], runner=lambda argv: [], todo=todo, workspace=None, base=self.dir)
        return json.loads((self.dir / 'followup-scan.json').read_text()), calls[1:]


class ReplayA20_1434(Base):
    def test_refused_reply_is_recorded_once_and_question_does_not_come_back(self):
        # 1) 질문이 열린 항목에 권한 밖 답이 온다: 스캔은 그 답을 이벤트로 낸다.
        answered = todo_row(replies=[REFUSED], unread=1)
        out, _ = self.scan([answered])
        self.assertEqual([e['commentId'] for e in out['events']], [self.reply])

        # 2) 오케스트레이터가 거절을 한 번에 기록한다.
        self.assertEqual(self.refuse(), 0)
        entry = self.read('state.json')['followup']['handled'][self.reply]
        self.assertEqual((entry['key'], entry['action'], entry['rule'], entry['slug']), (KEY, 'refused', RULE, SLUG))
        item = self.read('followup-queue.json')[KEY]
        self.assertEqual((item['waiting'], item['action'], item['runSheet']), ('human-risk', ACTION, SHEET))
        self.assertNotIn('question', item)
        self.assertNotIn('suggest', item)

        # 3) 스캔: 같은 답을 다시 내지 않고, 거절 한 줄과 '할 일:' 형식을 한 번만 쓴다. 권장 답·선택지는 없다.
        told = (f"공장 대기 — 받은 답 '시크릿 준비 직접해줘'은(는) 공장이 할 수 없어요 [{RULE}] — "
                f"할 일: {ACTION} · 실행지: {SHEET}{scan.ACTION_TAIL}")
        out, calls = self.scan([answered])
        self.assertEqual(out['events'], [])
        self.assertEqual(calls, [['wait', 'de78e7', told]])
        out, calls = self.scan([todo_row(replies=[REFUSED], waiting=told, asked=told)])
        self.assertEqual((out['events'], calls), ([], []))

        # 4) judge 판정: 켜져 있어도 모델을 부르지 않는다(11-09 재상신).
        with patch.dict(os.environ, JIRA_JUDGE_MODE='on'):
            self.assertEqual(judge.risk(self.dir, model=lambda data: self.fail(f'judge asked: {data}')), 0)

        # 5) 결정 수신기: 티켓 항목을 다시 열거나 바꾸지 않는다.
        polled = []

        def todo(args):
            polled.append(args)
            return [todo_row(replies=[REFUSED], waiting=told, asked=told)] if args[0] == 'list' else 'judge-item'
        with patch.dict(os.environ, JIRA_DECISION_SURFACE='app'), patch.object(inbox.dp, 'STATE', self.dir):
            quiet(inbox.poll, self.dir, todo=todo, item_cli=lambda args: None)
        self.assertFalse([c for c in polled if 'de78e7' in c], polled)

        # 6) 사람이 조치하고 답한다: 새 답은 이벤트로 가고 거절 문구를 다시 쓰지 않는다.
        done = todo_row(replies=[REFUSED, DONE], unread=1, asked=told)
        done_id = scan.todo_reply_id(done, DONE)
        out, calls = self.scan([done])
        self.assertEqual([e['commentId'] for e in out['events']], [done_id])
        self.assertEqual(calls, [])
        # 오케스트레이터가 확인 뒤 대기를 푼다: 할 일 형식은 지워지고 항목은 공장 진행으로 돌아간다.
        state.patch(self.dir, 'state.json', ['followup', 'handled', done_id],
                    {'key': KEY, 'action': 'resumed', 'at': '2026-11-10T18:30:00+09:00', 'slug': SLUG})
        state.patch(self.dir, 'followup-queue.json', [KEY], {'waiting': None, 'stage': 'dev-deploy'})
        item = self.read('followup-queue.json')[KEY]
        self.assertFalse({'action', 'runSheet', 'question', 'suggest'} & set(item))
        out, calls = self.scan([done])
        self.assertEqual(out['events'], [])
        self.assertEqual(calls, [['take', 'de78e7', '--json']])
        self.assertEqual(scan.refusals([done], self.read('state.json')['followup']['handled']), {})

    def test_refuse_twice_changes_nothing(self):
        self.assertEqual(self.refuse(), 0)
        names = ('state.json', 'followup-queue.json', 'judge-state.json')
        before = {name: (self.dir / name).read_bytes() for name in names}
        self.assertEqual(state.refuse(self.dir, KEY, self.reply, RULE, ACTION, SHEET), [])
        self.assertEqual({name: (self.dir / name).read_bytes() for name in names}, before)

    def test_judge_does_not_reescalate_the_refused_question(self):
        self.assertEqual(self.refuse(), 0)
        # 작업자가 같은 질문을 다시 써도 judge는 이미 본 질문으로 건너뛴다.
        state.patch(self.dir, 'followup-queue.json', [KEY], {'question': QUESTION, 'suggest': SUGGEST})
        with patch.dict(os.environ, JIRA_JUDGE_MODE='on'):
            self.assertEqual(judge.risk(self.dir, model=lambda data: self.fail('judge re-asked')), 0)

    def test_refuse_rejects_unknown_key_and_reply_of_another_key(self):
        state.patch(self.dir, 'state.json', ['followup', 'handled', 'todo:x:1'], {'key': 'PROJ-1', 'action': 'human'})
        with self.assertRaisesRegex(ValueError, 'another key'):
            state.refuse(self.dir, KEY, 'todo:x:1', RULE, ACTION)
        with self.assertRaisesRegex(ValueError, 'not in followup-queue'):
            state.refuse(self.dir, 'PROJ-9', self.reply, RULE, ACTION)
        with self.assertRaisesRegex(ValueError, 'one nonempty line'):
            state.refuse(self.dir, KEY, self.reply, ' ', ACTION)
        self.assertIn('question', self.read('followup-queue.json')[KEY])


def result(**changes):
    return dict(dict(status='waiting', summary='시크릿 필요', key=KEY, stage='tobe', waiting='human-risk',
                     question=None, suggest=None, humanAction={'line': ACTION, 'runSheet': SHEET},
                     artifacts=[], checks=[]), **changes)


class WorkerHumanAction(Base):
    def write(self, value):
        path = self.dir / 'result.json'
        path.write_text(json.dumps(value, ensure_ascii=False))
        return path

    def test_schema_validation(self):
        valid = lambda value: worker_runtime.valid_result(self.write(value), key=KEY)  # noqa: E731
        self.assertTrue(valid(result()))
        self.assertTrue(valid(result(humanAction={'line': ACTION, 'runSheet': None})))
        # 질문·권장 답과 함께, human-risk 밖, 모양이 틀림은 거부한다.
        self.assertFalse(valid(result(question=QUESTION, suggest=SUGGEST)))
        self.assertFalse(valid(result(suggest=SUGGEST)))
        self.assertFalse(valid(result(waiting='reporter')))
        self.assertFalse(valid(result(humanAction={'line': ' ', 'runSheet': None})))
        self.assertFalse(valid(result(humanAction={'line': ACTION})))
        self.assertFalse(valid(result(humanAction={'line': ACTION, 'runSheet': None, 'more': 1})))
        self.assertFalse(valid(result(humanAction=ACTION)))
        # 질문 형식은 그대로이고, humanAction이 없는 옛 결과도 받는다.
        self.assertTrue(valid(result(question=QUESTION, suggest=SUGGEST, humanAction=None)))
        old = result(question=QUESTION, suggest=SUGGEST)
        del old['humanAction']
        self.assertTrue(valid(old))

    def test_from_result_writes_action_form_and_rejects_invalid(self):
        def patch_from(value):
            return quiet(state.main, ['--dir', str(self.dir), 'patch', 'followup-queue.json', '--path', json.dumps([KEY]),
                                      '--json', '{"waiting": "human-risk"}', '--from-result', str(self.write(value))])
        self.assertEqual(patch_from(result(question=QUESTION)), 1)
        self.assertEqual(self.read('followup-queue.json')[KEY]['question'], QUESTION)
        self.assertEqual(patch_from(result()), 0)
        item = self.read('followup-queue.json')[KEY]
        self.assertEqual((item['action'], item['runSheet']), (ACTION, SHEET))
        self.assertFalse({'question', 'suggest'} & set(item))
        self.assertEqual(scan.todo_actions([(todo_row(asked=''), KEY)], self.read('followup-queue.json')),
                         [['wait', 'de78e7', f'공장 대기 — 할 일: {ACTION} · 실행지: {SHEET}{scan.ACTION_TAIL}']])
        self.assertEqual(patch_from(result(question=QUESTION, suggest=SUGGEST, humanAction=None)), 0)
        item = self.read('followup-queue.json')[KEY]
        self.assertEqual((item['question'], item['suggest']), (QUESTION, SUGGEST))
        self.assertNotIn('action', item)


if __name__ == '__main__':
    unittest.main()
