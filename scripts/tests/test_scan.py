import sys
import tempfile
import unittest
import contextlib
import io
import json
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from followup_scan import ME, select_events, select_intake  # noqa: E402
from followup_scan import find_related, select_todo, todo_actions, todo_reply_id  # noqa: E402
import followup_scan as scan  # noqa: E402

OTHER = 'someone-else'
SINCE = datetime.fromisoformat('2026-10-18T09:00:00+09:00')


def comment(cid, author, created, mention=False):
    content = [{'type': 'text', 'text': ' 님, 운영 배포 부탁드릴게요!'}]
    if mention:
        content.insert(0, {'type': 'mention', 'attrs': {'id': ME, 'text': '@운영자'}})
    return {'id': cid, 'author': {'accountId': author, 'displayName': author},
            'created': created, 'body': {'type': 'doc', 'content': [{'type': 'paragraph', 'content': content}]}}


def fields(*comments, assignee=ME):
    return {'summary': 's', 'status': {'name': '개발 검수 완료'},
            'assignee': {'accountId': assignee, 'displayName': assignee},
            'comment': {'comments': list(comments)}}


class SelectEventsTest(unittest.TestCase):
    def test_a20_1362_old_unanswered_mention_on_inflight_ticket_is_caught(self):
        # 2026-10-13 손다영 멘션(49439), 이후 내 답 없음, 72시간 창 밖
        f = fields(comment('49415', ME, '2026-10-12T15:40:20.951+0900'),
                   comment('49439', OTHER, '2026-10-13T14:02:10.043+0900', mention=True))
        events = select_events('PROJ-1362', f, SINCE, set(), inflight=True)
        self.assertEqual([e['commentId'] for e in events], ['49439'])
        self.assertTrue(events[0]['backstop'])
        self.assertFalse(events[0]['answeredByMeAfter'])

    def test_old_mention_not_inflight_stays_out(self):
        f = fields(comment('1', OTHER, '2026-10-13T14:02:10.043+0900', mention=True))
        self.assertEqual(select_events('PROJ-1', f, SINCE, set(), inflight=False), [])

    def test_old_mention_answered_or_handled_stays_out(self):
        f = fields(comment('1', OTHER, '2026-10-13T14:02:10.043+0900', mention=True),
                   comment('2', ME, '2026-10-13T15:00:00.000+0900'))
        self.assertEqual(select_events('PROJ-1', f, SINCE, set(), inflight=True), [])
        f = fields(comment('1', OTHER, '2026-10-13T14:02:10.043+0900', mention=True))
        self.assertEqual(select_events('PROJ-1', f, SINCE, {'1'}, inflight=True), [])

    def test_old_non_mention_on_inflight_ticket_stays_out(self):
        f = fields(comment('1', OTHER, '2026-10-13T14:02:10.043+0900'))
        self.assertEqual(select_events('PROJ-1', f, SINCE, set(), inflight=True), [])

    def test_window_rule_unchanged(self):
        f = fields(comment('1', OTHER, '2026-10-20T10:00:00.000+0900'),
                   comment('2', OTHER, '2026-10-20T11:00:00.000+0900', mention=True), assignee=OTHER)
        events = select_events('PROJ-1', f, SINCE, set())
        self.assertEqual([e['commentId'] for e in events], ['2'])
        self.assertFalse(events[0]['backstop'])


def ticket(status='백로그', summary='수학 AI 맞춤학습 무한 로딩', assignee=ME, *comments):
    category = 'new' if status in ('백로그', '해야 할 일') else 'indeterminate'
    return {'summary': summary, 'status': {'name': status, 'statusCategory': {'key': category}}, 'created': '2026-10-20T10:00:00.000+0900',
            'assignee': {'accountId': assignee, 'displayName': assignee},
            'reporter': {'accountId': 'planner', 'displayName': '기획자'},
            'description': {'type': 'doc', 'content': [{'type': 'paragraph', 'content': [
                {'type': 'text', 'text': '페이지 이동 시 로딩이 끝나지 않음'}]}]},
            'comment': {'comments': list(comments)}}


class SelectIntakeTest(unittest.TestCase):
    def test_backlog_ticket_without_comments_becomes_one_intake_event(self):
        for status in ('백로그', '해야 할 일'):
            events = select_intake('PROJ-1405', ticket(status), set(), set())
            self.assertEqual([e['commentId'] for e in events], ['intake:PROJ-1405'])
            e = events[0]
            self.assertTrue(e['intake'])
            self.assertEqual((e['reporterId'], e['text']), ('planner', '페이지 이동 시 로딩이 끝나지 않음'))
            self.assertFalse(e['answeredByMeAfter'] or e['lastCommentByMe'])

    def test_hold_queued_handled_other_status_or_assignee_stay_out(self):
        self.assertEqual(select_intake('PROJ-1385', ticket(summary='[보류][WEB] 다중학급'), set(), set()), [])
        self.assertEqual(select_intake('PROJ-1405', ticket(), set(), {'PROJ-1405'}), [])
        self.assertEqual(select_intake('PROJ-1405', ticket(), {'intake:PROJ-1405'}, set()), [])
        self.assertEqual(select_intake('PROJ-1405', ticket('진행 중'), set(), set()), [])
        self.assertEqual(select_intake('PROJ-1405', ticket('백로그', 's', OTHER), set(), set()), [])
        self.assertEqual(select_intake('OTHER-139', ticket('해야 할 일'), set(), set()), [])

    def test_my_last_comment_is_flagged_not_dropped(self):
        f = ticket('백로그', 's', ME, comment('1', OTHER, '2026-10-13T14:00:00.000+0900'),
                   comment('2', ME, '2026-10-13T15:00:00.000+0900'))
        self.assertTrue(select_intake('PROJ-1404', f, set(), set())[0]['lastCommentByMe'])


class RelatedIntakeTest(unittest.TestCase):
    def test_other_assignee_recent_issue_is_attached_and_unrelated_is_not(self):
        calls = []
        def search(argv):
            calls.append(argv)
            def issue(key, summary):
                return {'key': key, 'fields': {'summary': summary, 'assignee': {'accountId': OTHER}}}
            return [issue('PROJ-1406', '사용자 진단 첫 진입 미응시 안내'),
                    issue('PROJ-1407', '사용자 진단 결과 안내'),
                    issue('PROJ-1405', '사용자 진단 이동'),
                    issue('OTHER-3', '사용자 진단 이동'),
                    issue('PROJ-1408', '로그인 비밀번호')]
        related = find_related('PROJ-1405', '사용자 진단 다음 무한 로딩', search=search)
        self.assertEqual([r['key'] for r in related], ['PROJ-1406', 'PROJ-1407'])
        jql = calls[0][calls[0].index('--jql') + 1]
        self.assertNotIn('assignee', jql)
        self.assertIn('created >= -7d OR updated >= -7d', jql)
        self.assertIn('--paginate', calls[0])
        # acli search 는 created·updated 필드를 거부한다.
        fields = calls[0][calls[0].index('--fields') + 1].split(',')
        self.assertFalse({'created', 'updated'} & set(fields))

    def test_slug_names_and_documents_match_key_or_significant_tokens(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            by_key = root / '.scratch' / 'ailrn-custom-loading-20261021'
            by_key.mkdir(parents=True)
            (by_key / 'dev.md').write_text('PROJ-1405 사용자 확인')
            by_token = root / '.sdlc' / 'work' / '진단-미응시-안내'
            by_token.mkdir(parents=True)
            unrelated = root / '.scratch' / 'core-web-api'
            unrelated.mkdir()
            body_token_only = root / '.scratch' / 'other-work'
            body_token_only.mkdir()
            (body_token_only / 'note.md').write_text('진단 결과를 다시 봤다')
            related = find_related('PROJ-1405', '[WEB] 사용자 진단 무한 로딩', search=lambda argv: [], workspace=root)
            self.assertEqual({r['path'] for r in related}, {str(by_key), str(by_token)})
            self.assertEqual({r['source'] for r in related}, {'slug'})

    def test_intake_search_is_injected_and_only_called_for_eligible_ticket(self):
        calls = []
        def related(key, summary):
            calls.append((key, summary))
            return [{'source': 'jira', 'key': 'PROJ-1406'}]
        result = select_intake('PROJ-1405', ticket(), set(), set(), related_search=related)
        self.assertEqual(result[0]['related'], [{'source': 'jira', 'key': 'PROJ-1406'}])
        self.assertEqual(len(calls), 1)
        self.assertEqual(select_intake('PROJ-1405', ticket(), set(), {'PROJ-1405'}, related_search=related), [])
        self.assertEqual(len(calls), 1)

    def test_related_search_failure_remains_visible_on_intake(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            def runner(argv):
                if argv[0] == 'search':
                    return [{'key': 'PROJ-1405'}] if 'statusCategory' in argv[argv.index('--jql') + 1] else []
                return {'fields': ticket()}
            def failed(key, summary):
                raise RuntimeError('must not print external details')
            with contextlib.redirect_stdout(io.StringIO()):
                scan.main([], runner=runner, related_search=failed, workspace=base, base=base)
            output = json.loads((base / 'followup-scan.json').read_text())
            self.assertTrue(output['events'][0]['relatedIncomplete'])
            self.assertEqual(output['errors'], [{'scope': 'related:PROJ-1405', 'error': 'related-search-failed'}])



def todo_row(**over):
    # 2026-10-26 ~/.sdlc/todo.md 의 PROJ-1370 항목: 11:31 sdlc 대기 뒤 12:53 내 답 "continue"
    row = {'id': '2e45ab', 'title': 'PROJ-1370 설정 탭 메뉴명이 같은 화면에서 개발계·스테이징마다 다르게 노출되는지 확인',
           'link': {'label': 'PROJ-1370', 'url': 'https://jira.example.com/browse/PROJ-1370'},
           'slug': 'proj-1370-ai-tab-name-env-diff', 'state': 'NEEDS-HUMAN', 'status': 'workable',
           'action': 'audit API 응답 관찰 미완료.', 'replies': [{'at': '2026-10-26 12:53', 'who': 'human', 'what': 'reply — continue'}]}
    row.update(over)
    return row


class SelectTodoTest(unittest.TestCase):
    def test_a20_1370_unread_reply_becomes_my_instruction_event(self):
        events, settled = select_todo([todo_row()], set())
        self.assertEqual(settled, [])
        self.assertEqual(len(events), 1)
        e = events[0]
        self.assertEqual((e['key'], e['source'], e['todoId'], e['text']), ('PROJ-1370', 'todo', '2e45ab', 'continue'))
        self.assertTrue(e['commentId'].startswith('todo:2e45ab:'))
        self.assertEqual(e['commentId'], todo_reply_id(todo_row(), todo_row()['replies'][0]))

    def test_handled_reply_settles_the_row(self):
        row = todo_row()
        handled = {todo_reply_id(row, row['replies'][0])}
        self.assertEqual(select_todo([row], handled), ([], [(row, 'PROJ-1370')]))

    def test_unlinked_done_and_agent_answers_raise_no_event(self):
        agent_only = todo_row(replies=[{'at': '2026-10-26 12:00', 'who': 'agent', 'what': 'chat answer — x'}])
        rows = [todo_row(link=None, title='개인 메모'), todo_row(status='done'), agent_only]
        self.assertEqual(select_todo(rows, set()), ([], [(agent_only, 'PROJ-1370')]))
        self.assertEqual(select_todo([todo_row(link=None)], set())[0][0]['key'], 'PROJ-1370')

    def test_queue_wait_is_mirrored_and_cleared(self):
        # 2026-10-26 PROJ-1348: 큐 human-risk, sdlc READY, to-do 대기 없음 -> 화면 '에이전트 진행 중'
        ready = todo_row(id='0e801e', state='READY', replies=[], waiting='', unread=0,
                         link={'label': 'PROJ-1348', 'url': 'u'})
        queue = {'PROJ-1348': {'waiting': 'human-risk', 'note': '운영 영향\n검토 필요'}}
        wait = ['wait', '0e801e', '공장 대기 — human-risk: 운영 영향 검토 필요']
        self.assertEqual(todo_actions([(ready, 'PROJ-1348')], queue), [wait])
        mirrored = dict(ready, waiting=wait[2])
        self.assertEqual(todo_actions([(mirrored, 'PROJ-1348')], queue), [])
        self.assertEqual(todo_actions([(dict(mirrored, unread=1), 'PROJ-1348')], queue), [wait])
        cleared = {'PROJ-1348': {'waiting': None}}
        self.assertEqual(todo_actions([(mirrored, 'PROJ-1348')], cleared), [['resume', '0e801e', '공장 큐 대기 해제']])
        self.assertEqual(todo_actions([(dict(ready, waiting='which project?'), 'PROJ-1348')], {}), [])
        self.assertEqual(todo_actions([(dict(ready, unread=1), 'PROJ-1348')], {}), [['take', '0e801e', '--json']])

    def test_main_mirrors_queue_and_todo_failure_keeps_jira_scan(self):
        row = dict(todo_row(), waiting='', unread=0)
        cid = todo_reply_id(row, row['replies'][0])
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            (base / 'state.json').write_text(json.dumps({'followup': {'handled': {cid: {'key': 'PROJ-1370'}}}}))
            (base / 'followup-queue.json').write_text(json.dumps({'PROJ-1370': {'waiting': 'human-risk', 'note': 'audit 브라우저 제어권'}}))
            calls = []
            def todo(args):
                calls.append(args)
                return [row] if args[0] == 'list' else None
            with contextlib.redirect_stdout(io.StringIO()):
                scan.main([], runner=lambda argv: [], todo=todo, workspace=base, base=base)
            output = json.loads((base / 'followup-scan.json').read_text())
            self.assertEqual(calls, [['list', '--json'], ['wait', '2e45ab', '공장 대기 — human-risk: audit 브라우저 제어권']])
            self.assertEqual(output['todoSynced'], [['wait', '2e45ab']])
            def broken(args):
                raise RuntimeError('node missing')
            with contextlib.redirect_stdout(io.StringIO()):
                scan.main([], runner=lambda argv: [], todo=broken, workspace=base, base=base)
            output = json.loads((base / 'followup-scan.json').read_text())
            self.assertEqual(output['errors'], [{'scope': 'todo', 'error': 'node missing'}])


if __name__ == '__main__':
    unittest.main()
