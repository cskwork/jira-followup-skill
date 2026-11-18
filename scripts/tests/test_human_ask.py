"""human-risk 질문·권장 답(2026-11-05 사용자 지시, PROJ-1438): 작업 기록 대신 사용자가 정할 것 한 문장과
그대로 보낼 권장 답 한 줄을 할 일 앱에 보인다. to-do 명령은 가짜로 받아 적는다."""

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import decision_inbox as inbox  # noqa: E402
import decision_page as dp  # noqa: E402
import followup_scan as scan  # noqa: E402
import state_update as state  # noqa: E402
import worker_runtime  # noqa: E402

QUESTION = '콘텐츠 미리보기를 로그인 없이 지금처럼 둘까요, 이번에 인증을 붙일까요?'
SUGGEST = '콘텐츠 미리보기는 로그인 없이 지금처럼 두고, HLS 파일은 이번 범위에서 뺀다'
LOG_NOTE = "2026-11-03 to-do 답 'continue' 처리 | check_adf PASS · 개발자 노트 47 갱신, 무토큰 미리보기 판단 필요"


def result(**changes):
    return dict(status='waiting', summary='판단 필요', key='PROJ-1438', stage='build', waiting='human-risk',
                question=QUESTION, suggest=SUGGEST, artifacts=[], checks=[], **changes)


def row(**changes):
    base = dict(id='b1', title='PROJ-1438 콘텐츠 미리보기', status='open', waiting='', asked='', unread=0,
                replies=[], suggest='', link={'label': 'PROJ-1438', 'url': 'u'})
    return dict(base, **changes)


class WorkerResult(unittest.TestCase):
    def check(self, value):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'result.json'
            path.write_text(json.dumps(value, ensure_ascii=False))
            return worker_runtime.valid_result(path, key='PROJ-1438')

    def test_human_risk_needs_question_and_suggest(self):
        self.assertTrue(self.check(result()))
        for name in ('question', 'suggest'):
            for bad in (None, '', '   '):
                with self.subTest(name=name, bad=bad):
                    self.assertFalse(self.check(dict(result(), **{name: bad})))
        # 다른 대기·완료는 null이면 된다.
        self.assertTrue(self.check(dict(result(), waiting='reporter', question=None, suggest=None)))
        self.assertTrue(self.check(dict(result(), status='done', waiting=None, question=None, suggest=None)))
        # 엄격 출력 스키마: 두 필드도 빠지면 안 된다.
        missing = result()
        del missing['suggest']
        self.assertFalse(self.check(missing))

    def test_schema_lists_every_property_as_required(self):
        schema = json.loads((Path(worker_runtime.__file__).with_name('worker-result.schema.json')).read_text())
        self.assertEqual(set(schema['required']), set(schema['properties']))
        self.assertEqual(schema['properties']['question']['type'], ['string', 'null'])
        self.assertEqual(schema['properties']['suggest']['type'], ['string', 'null'])


class QueueWrite(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        state.atomic_json(self.directory / 'followup-queue.json', {'PROJ-1438': {'waiting': None, 'note': '기록'}})

    def queue(self):
        return state.read_object(self.directory / 'followup-queue.json')['PROJ-1438']

    def test_setting_human_risk_requires_question_and_suggest(self):
        with self.assertRaisesRegex(ValueError, 'question and suggest'):
            state.patch(self.directory, 'followup-queue.json', ['PROJ-1438'], {'waiting': 'human-risk'})
        with self.assertRaisesRegex(ValueError, 'question and suggest'):
            state.patch(self.directory, 'followup-queue.json', ['PROJ-1438'],
                        {'waiting': 'human-risk', 'question': QUESTION, 'suggest': ' '})
        self.assertIsNone(self.queue()['waiting'])
        # 다른 파일의 waiting 필드는 검사하지 않는다.
        state.patch(self.directory, 'state.json', ['x'], {'waiting': 'human-risk'})

    def test_cli_options_set_them_and_clearing_wait_drops_them(self):
        code = state.main(['--dir', str(self.directory), 'patch', 'followup-queue.json', '--path', '["PROJ-1438"]',
                           '--json', '{"waiting": "human-risk"}', '--question', QUESTION, '--suggest', SUGGEST])
        self.assertEqual(code, 0)
        self.assertEqual((self.queue()['question'], self.queue()['suggest']), (QUESTION, SUGGEST))
        # 이미 질문이 있는 항목은 note만 바꿔도 된다.
        state.patch(self.directory, 'followup-queue.json', ['PROJ-1438'], {'note': '기록 | 새 기록', 'waiting': 'human-risk'})
        self.assertEqual(self.queue()['question'], QUESTION)
        state.patch(self.directory, 'followup-queue.json', ['PROJ-1438'], {'waiting': None})
        self.assertNotIn('question', self.queue())
        self.assertNotIn('suggest', self.queue())

    def test_cli_rejects_human_risk_without_question(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), contextlib.redirect_stdout(io.StringIO()):
            code = state.main(['--dir', str(self.directory), 'patch', 'followup-queue.json', '--path', '["PROJ-1438"]',
                               '--json', '{"waiting": "human-risk"}'])
        self.assertEqual(code, 1)
        self.assertIn('--question', stderr.getvalue())

    def test_switch_to_other_wait_drops_them(self):
        state.patch(self.directory, 'followup-queue.json', ['PROJ-1438'],
                    {'waiting': 'human-risk', 'question': QUESTION, 'suggest': SUGGEST})
        state.patch(self.directory, 'followup-queue.json', ['PROJ-1438'], {'waiting': 'reporter'})
        self.assertNotIn('question', self.queue())


class TodoActions(unittest.TestCase):
    queue = {'PROJ-1438': {'waiting': 'human-risk', 'note': LOG_NOTE, 'question': QUESTION, 'suggest': SUGGEST}}
    shown = f'공장 대기 — {QUESTION}'

    def test_question_is_shown_without_log_and_suggest_is_offered(self):
        actions = scan.todo_actions([(row(), 'PROJ-1438')], self.queue)
        self.assertEqual(actions, [['wait', 'b1', self.shown], ['suggest', 'b1', SUGGEST]])
        for word in ('human-risk', 'check_adf', '개발자 노트', 'PASS'):
            self.assertNotIn(word, actions[0][2])
        # 같은 대기·같은 권장 답이면 아무것도 안 한다.
        self.assertEqual(scan.todo_actions([(row(waiting=self.shown, suggest=SUGGEST), 'PROJ-1438')], self.queue), [])

    def test_reply_without_effect_reasks_question_not_log(self):
        # 답글은 대기와 권장 답을 지운다(sdlc-kit). 처리했는데 대기가 그대로면 질문으로 다시 묻고 권장 답을 다시 채운다.
        answered = row(asked=self.shown, unread=1,
                       replies=[{'at': '2026-11-03 14:07', 'who': 'human', 'what': 'reply — 승인'}])
        told = f"공장 대기 — 받은 답 '승인'(으)로는 바뀐 것이 없어요 — {QUESTION}"
        self.assertEqual(scan.todo_actions([(answered, 'PROJ-1438')], self.queue),
                         [['wait', 'b1', told], ['suggest', 'b1', SUGGEST]])
        # 알린 뒤 다음 스캔은 같은 대기로 본다.
        settled = row(waiting=told, asked=told, suggest=SUGGEST)
        self.assertEqual(scan.todo_actions([(settled, 'PROJ-1438')], self.queue), [])

    def test_missing_question_keeps_old_text_and_resume_clears_suggest(self):
        legacy = {'PROJ-1438': {'waiting': 'human-risk', 'note': '첫 기록 | 범위 결정 필요'}}
        self.assertEqual(scan.todo_actions([(row(), 'PROJ-1438')], legacy),
                         [['wait', 'b1', '공장 대기 — human-risk: 범위 결정 필요']])
        waiting = row(waiting=self.shown, suggest=SUGGEST)
        self.assertEqual(scan.todo_actions([(waiting, 'PROJ-1438')], {'PROJ-1438': {'waiting': None}}),
                         [['resume', 'b1', '공장 큐 대기 해제'], ['suggest', 'b1', '']])
        # 공장 대기가 아닌 항목의 권장 답은 건드리지 않는다.
        self.assertEqual(scan.todo_actions([(row(suggest='남의 권장'), 'PROJ-1438')], {}), [])

    def test_open_decision_item_is_pointed_to_and_suggest_cleared(self):
        # 2026-11-06 PROJ-1439: 결정은 '[결정 D62]' 항목에만 선택지가 있는데 티켓 항목은 질문만 보여 승인할 곳을 못 찾았다.
        decision = dict(id='d62', title='[결정 D62] PROJ-1438 · 미리보기 인증 결정', status='waiting',
                        link={'label': 'PROJ-1438', 'url': 'u'})
        done = dict(decision, id='d61', title='[결정 D61] PROJ-1438 · 지난 결정', status='done')
        decisions = scan.open_decisions([row(), decision, done])
        self.assertEqual(decisions, {'PROJ-1438': '결정 D62'})
        pointed = f"공장 대기 — {scan.decision_where('결정 D62')} · {QUESTION}"
        self.assertEqual(scan.todo_actions([(row(suggest=SUGGEST), 'PROJ-1438')], self.queue, decisions=decisions),
                         [['wait', 'b1', pointed], ['suggest', 'b1', '']])
        self.assertEqual(scan.todo_actions([(row(waiting=pointed), 'PROJ-1438')], self.queue, decisions=decisions), [])
        # 스테이징 승인·환경 조치 대기는 결정 항목을 가리키지 않는다.
        env = {'PROJ-1438': {'waiting': 'env-human', 'note': 'VPN'}}
        self.assertNotIn("'[결정 D62]'", scan.todo_actions([(row(), 'PROJ-1438')], env, decisions=decisions)[0][2])

    def test_alert_points_ticket_item_to_open_decision(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            (directory / 'followup-queue.json').write_text(json.dumps(self.queue, ensure_ascii=False))
            rows = [row(), dict(id='d62', title='[결정 D62] PROJ-1438 · 결정', status='waiting',
                                link={'label': 'PROJ-1438', 'url': 'u'})]
            calls = []

            def todo(args):
                calls.append(args)
                return rows if args[0] == 'list' else None
            with patch.dict('os.environ', JIRA_DECISION_SURFACE='app'), patch.object(inbox, 'ticket_phase', return_value=''):
                self.assertTrue(inbox.alert('human-risk: 판단 필요', directory, key='PROJ-1438', todo=todo))
            self.assertIn(['wait', 'b1', f"공장 대기 — {scan.decision_where('결정 D62')} · {QUESTION}"], calls)
            self.assertFalse([c for c in calls if c[0] == 'suggest'])
            brief = (directory / 'decision-pages' / 'ticket-PROJ-1438.brief.md').read_text()
            self.assertIn("'[결정 D62]' 항목에서", brief)
            self.assertNotIn('**AI pick:**', brief)

    def test_main_keeps_wait_when_old_kit_has_no_suggest(self):
        item = row()
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            (base / 'followup-queue.json').write_text(json.dumps(self.queue, ensure_ascii=False))
            calls = []

            def todo(args):
                calls.append(args)
                if args[0] == 'suggest':
                    raise RuntimeError('unknown command: suggest')
                return [item] if args[0] == 'list' else None
            with contextlib.redirect_stdout(io.StringIO()), patch.dict('os.environ', JIRA_DECISION_SURFACE='html'):
                scan.main([], runner=lambda argv: [], todo=todo, workspace=base, base=base)
            output = json.loads((base / 'followup-scan.json').read_text())
        self.assertEqual(calls[1:], [['wait', 'b1', self.shown], ['suggest', 'b1', SUGGEST]])
        self.assertEqual(output['todoSynced'], [['wait', 'b1']])
        self.assertEqual(output['errors'], [{'scope': 'todo-suggest', 'error': 'unknown command: suggest'}])


class Briefs(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)

    def ticket_brief(self, item):
        (self.directory / 'followup-queue.json').write_text(json.dumps({'PROJ-1438': item}, ensure_ascii=False))
        with patch.object(inbox, 'ticket_phase', return_value=''):
            return inbox.ticket_brief('PROJ-1438', self.directory, '콘텐츠 미리보기').read_text()

    def test_ticket_brief_your_call_is_question_and_ai_pick_is_suggest(self):
        text = self.ticket_brief({'waiting': 'human-risk', 'stage': 'build', 'note': LOG_NOTE,
                                  'question': QUESTION, 'suggest': SUGGEST})
        self.assertIn(f'- **Your call:** {QUESTION}', text)
        self.assertIn(f'- **AI pick:** AI 권장: {SUGGEST}', text)
        # 공장 상태·최근 기록은 '자세히 보기' 아래에 남는다.
        self.assertIn('## 공장 상태', text)
        self.assertIn('check_adf PASS', text.split('## 최근 기록', 1)[1])

    def test_ticket_brief_without_question_keeps_wait_guide(self):
        text = self.ticket_brief({'waiting': 'human-risk', 'note': LOG_NOTE})
        self.assertIn(f"- **Your call:** {inbox.WAITS['human-risk']}", text)
        self.assertNotIn('**AI pick:**', text)

    def open(self, spec):
        calls = []

        def todo(args):
            calls.append(args)
            return 'item1' if args[0] == 'add' else [] if args[0] == 'list' else None
        path = self.directory / f"{spec['kind']}.json"
        path.write_text(json.dumps(spec, ensure_ascii=False))
        with patch.object(inbox, 'ticket_phase', return_value=''):
            inbox.register('spec', str(path), self.directory, todo=todo)
        return calls

    def test_decision_items_offer_recommended_reply(self):
        choice = {'kind': 'choice', 'humanReason': 'product-scope', 'key': 'PROJ-1', 'title': '처리 방법',
                  'question': '어떻게 할까요?', 'recommendReason': '근거', 'recommended': 'skip',
                  'options': [{'id': 'wait', 'label': '기다림'}, {'id': 'skip', 'label': '제외 — 이번 범위에서 뺀다'}]}
        self.assertIn(['suggest', 'item1', '제외'], self.open(choice))
        sql = {'kind': 'sql-run', 'key': 'PROJ-2', 'title': '보정 SQL', 'config': 'fake_stg',
               'expect_host': 'fake-stg-', 'file': '/tmp/fake.sql'}
        self.assertIn(['suggest', 'item1', '승인'], self.open(sql))
        stg = self.open({'kind': 'stg-approval', 'key': 'PROJ-3', 'title': '스테이징 승인'})
        self.assertEqual(stg[-1], ['suggest', 'item1', '승인'])

    def test_old_kit_without_suggest_is_ignored(self):
        def todo(args):
            if args[0] == 'suggest':
                raise RuntimeError('unknown command: suggest')
            return 'item1' if args[0] == 'add' else None
        with contextlib.redirect_stderr(io.StringIO()) as stderr:
            inbox.offer_suggest('item1', '승인', todo=todo)
        self.assertIn('suggest ignored', stderr.getvalue())


if __name__ == '__main__':
    unittest.main()
