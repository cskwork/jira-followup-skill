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
    def test_proj_1362_old_unanswered_mention_on_inflight_ticket_is_caught(self):
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

    def test_slug_walk_skips_checkouts_and_runs_once_per_scan(self):
        # 2026-10-29: worktree `.git`(파일)을 그대로 훑어 접수 티켓마다 56초, 스캔이 300·570초 제한을 넘겼다.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            slug = root / '.scratch' / 'other-work'
            for checkout, git in (('api', 'file'), ('web', 'dir')):
                (slug / checkout / 'docs').mkdir(parents=True)
                (slug / checkout / 'docs' / 'note.md').write_text('PROJ-1405 남의 문서')
                (slug / checkout / '.git').mkdir() if git == 'dir' else (slug / checkout / '.git').write_text('gitdir: x')
            self.assertEqual(find_related('PROJ-1405', '무관', search=lambda argv: [], workspace=root), [])
            (slug / 'PLAN.md').write_text('PROJ-1405 계획')
            self.assertEqual([r['matchedKey'] for r in find_related('PROJ-1405', '무관', search=lambda argv: [], workspace=root)], [True])

            walks = []
            real = scan.slug_texts
            def counted(workspace):
                walks.append(workspace)
                return real(workspace)
            def runner(argv):
                if argv[0] == 'search':
                    jql = argv[argv.index('--jql') + 1]
                    return [{'key': 'PROJ-1405'}, {'key': 'PROJ-1406'}] if 'statusCategory' in jql else []
                return {'fields': ticket()}
            scan.slug_texts = counted
            try:
                with contextlib.redirect_stdout(io.StringIO()):
                    scan.main([], runner=runner, workspace=root, base=root)
            finally:
                scan.slug_texts = real
            self.assertEqual(len(walks), 1)
            events = json.loads((root / 'followup-scan.json').read_text())['events']
            self.assertEqual([e['key'] for e in events], ['PROJ-1405', 'PROJ-1406'])

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

    def scan_related_failure(self, message):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            def runner(argv):
                if argv[0] == 'search':
                    return [{'key': 'PROJ-1405'}] if 'statusCategory' in argv[argv.index('--jql') + 1] else []
                return {'fields': ticket()}
            def failed(key, summary):
                raise RuntimeError(message)
            with contextlib.redirect_stdout(io.StringIO()):
                scan.main([], runner=runner, related_search=failed, workspace=base, base=base)
            return json.loads((base / 'followup-scan.json').read_text())

    def test_related_search_failure_remains_visible_on_intake(self):
        message = "acli rc=2 fields 'created, updated' are not allowed"
        output = self.scan_related_failure(message)
        self.assertTrue(output['events'][0]['relatedIncomplete'])
        self.assertEqual(output['events'][0]['related'], [])
        self.assertEqual(output['errors'], [{'scope': 'related:PROJ-1405',
                                            'error': 'related-search-failed: ' + message}])

    def test_related_search_failure_limits_exception_detail_to_300_characters(self):
        message = '관련 검색 오류 ' + 'x' * 400
        output = self.scan_related_failure(message)
        self.assertTrue(output['events'][0]['relatedIncomplete'])
        self.assertEqual(output['events'][0]['related'], [])
        self.assertEqual(output['errors'], [{'scope': 'related:PROJ-1405',
                                            'error': 'related-search-failed: ' + message[:300]}])



def todo_row(**over):
    # 2026-10-26 ~/.sdlc/todo.md 의 PROJ-1370 항목: 11:31 sdlc 대기 뒤 12:53 내 답 "continue"
    row = {'id': '2e45ab', 'title': 'PROJ-1370 설정 탭 메뉴명이 같은 화면에서 개발계·스테이징마다 다르게 노출되는지 확인',
           'link': {'label': 'PROJ-1370', 'url': 'https://jira.example.com/browse/PROJ-1370'},
           'slug': 'proj-1370-ai-tab-name-env-diff', 'state': 'NEEDS-HUMAN', 'status': 'workable',
           'action': 'stg API 응답 관찰 미완료.', 'replies': [{'at': '2026-10-26 12:53', 'who': 'human', 'what': 'reply — continue'}]}
    row.update(over)
    return row


class SelectTodoTest(unittest.TestCase):
    def test_proj_1370_unread_reply_becomes_my_instruction_event(self):
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
        # 덧붙인 최신 기록이 보드에 보여야 한다(2026-10-30 PROJ-1353: 10-19 첫 기록만 보임).
        appended = {'PROJ-1348': {'waiting': 'human-risk', 'note': '2026-10-19 첫 기록 | 2026-10-27 최신 기록'}}
        self.assertEqual(todo_actions([(mirrored, 'PROJ-1348')], appended),
                         [['wait', '0e801e', '공장 대기 — human-risk: 2026-10-27 최신 기록']])

    def test_reply_without_effect_is_told_once_not_rewritten(self):
        # 2026-11-03 PROJ-1438: '승인'·'네' 답 뒤 같은 대기 문구가 6번 다시 올라와 같은 답을 반복했다.
        queue = {'PROJ-1438': {'waiting': 'human-risk', 'note': '콘텐츠 미리보기 인증 처리 범위 결정 필요'}}
        plain = '공장 대기 — human-risk: 콘텐츠 미리보기 인증 처리 범위 결정 필요'
        answered = todo_row(id='b1', waiting='', asked=plain, unread=1, link={'label': 'PROJ-1438', 'url': 'u'},
                            replies=[{'at': '2026-11-03 14:07', 'who': 'human', 'what': 'reply — 승인'}])
        told = "공장 대기 — human-risk: 받은 답 '승인'(으)로는 바뀐 것이 없어요 — 콘텐츠 미리보기 인증 처리 범위 결정 필요"
        # 답 직전 대기와 같으면(작업자가 처리했지만 바뀐 것 없음) 안 먹혔다고 알린다.
        self.assertEqual(todo_actions([(answered, 'PROJ-1438')], queue), [['wait', 'b1', told]])
        # 알린 뒤 다음 스캔은 같은 대기로 보고 다시 쓰지 않는다.
        self.assertEqual(todo_actions([(dict(answered, waiting=told, asked=told, unread=0, replies=[]), 'PROJ-1438')], queue), [])
        # 알린 뒤 또 같은 답이 와도 같은 알림 한 번(머리말을 빼고 비교한다).
        again = dict(answered, asked=told, replies=[{'at': '2026-11-03 15:35', 'who': 'human', 'what': 'reply — 승인'}])
        self.assertEqual(todo_actions([(again, 'PROJ-1438')], queue), [['wait', 'b1', told]])
        # 옛 sdlc-kit(asked 없음)은 예전처럼 같은 대기를 다시 쓴다.
        self.assertEqual(todo_actions([(dict(answered, asked=None), 'PROJ-1438')], queue), [['wait', 'b1', plain]])
        # 작업자가 새 기록을 남기면 그 기록이 대기가 된다.
        moved = {'PROJ-1438': {'waiting': 'human-risk', 'note': '첫 기록 | 범위=콘텐츠 제외로 진행, 승인 기록'}}
        self.assertEqual(todo_actions([(answered, 'PROJ-1438')], moved),
                         [['wait', 'b1', '공장 대기 — human-risk: 범위=콘텐츠 제외로 진행, 승인 기록']])

    def test_reporter_wait_is_others_not_your_turn(self):
        # 2026-11-04 이력: 기획·QA 회신 대기(reporter)에 내 답글 49건 — 내가 할 일이 없는데 '확인 필요'에 떴다.
        queue = {'PROJ-1442': {'waiting': 'reporter', 'note': "가드 2026-11-03: Jira '개발 QA 요청'라 QA 회신 대기(reporter)로 바꿈"}}
        text = "기획·QA 회신 대기 — 가드 2026-11-03: Jira '개발 QA 요청'라 QA 회신 대기(reporter)로 바꿈"
        old = todo_row(id='c1', waiting="공장 대기 — reporter: 옛 문구", unread=0, replies=[], link={'label': 'PROJ-1442', 'url': 'u'})
        self.assertEqual(todo_actions([(old, 'PROJ-1442')], queue), [['others', 'c1', text]])
        shown = dict(old, waiting='', others=text)
        self.assertEqual(todo_actions([(shown, 'PROJ-1442')], queue), [])
        # 내 답(이미 처리됨)은 읽음 처리 후 다시 다른 사람 대기로 둔다(답글이 others를 지운다).
        replied = dict(shown, others='', unread=1)
        self.assertEqual(todo_actions([(replied, 'PROJ-1442')], queue), [['take', 'c1', '--json'], ['others', 'c1', text]])
        # 큐 대기가 풀리면 지운다.
        self.assertEqual(todo_actions([(shown, 'PROJ-1442')], {'PROJ-1442': {'waiting': None}}), [['resume', 'c1', '공장 큐 대기 해제']])

    def test_env_wait_shows_the_action_and_auto_resume(self):
        queue = {'PROJ-1463': {'waiting': 'env-human', 'needs': 'jira,dev-db', 'note': '[plan] 비로그인·6개 값 유지'}}
        env = {'PROJ-1463': 'Switch VPN to dev; type OTP in Terminal: open -a Terminal dev.command'}
        row = todo_row(id='d1', waiting='', unread=0, replies=[], link={'label': 'PROJ-1463', 'url': 'u'})
        self.assertEqual(todo_actions([(row, 'PROJ-1463')], queue, env), [['wait', 'd1',
            '공장 대기 — env-human: 할 일: Switch VPN to dev; type OTP in Terminal: open -a Terminal dev.command'
            ' · 조치하면 공장이 확인 뒤 스스로 이어갑니다(답글 필요 없음)'], ['suggest', 'd1', '조치했어요'],
            ['run', 'd1', 'open -a Terminal dev.command']])
        # 답 칸 권장 답·실행 명령은 이미 같으면 다시 보내지 않는다.
        same = [a[0] for a in todo_actions([(dict(row, suggest='조치했어요', run='open -a Terminal dev.command'), 'PROJ-1463')], queue, env)]
        self.assertFalse({'suggest', 'run'} & set(same))
        # 공백 든 경로는 따옴표째, 뒤에 붙는 안내는 빼고 명령만 뽑는다.
        self.assertEqual(scan.terminal_command("[Jira follow-up line 조치 필요]\nSwitch VPN to stg; type OTP in Terminal: open -a Terminal '/a b/stg.command' 시 진행 가능"),
                         "open -a Terminal '/a b/stg.command'")
        self.assertEqual(scan.terminal_command('VPN 없이 조치'), '')
        # 가드가 조치를 못 찾으면 예전처럼 기록을 보인다.
        self.assertEqual(todo_actions([(row, 'PROJ-1463')], queue, {}), [['wait', 'd1', '공장 대기 — env-human: [plan] 비로그인·6개 값 유지'], ['suggest', 'd1', '조치했어요']])
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / 'guard-last.json').write_text(json.dumps({'waitingHuman': {env['PROJ-1463']: ['PROJ-1463']}}))
            self.assertEqual(scan.env_actions(directory), env)
            self.assertEqual(scan.env_actions(Path(directory) / 'missing'), {})

    def test_stg_wait_on_ticket_points_to_the_approval_item(self):
        queue = {'PROJ-1451': {'waiting': 'stg-approval', 'note': 'stg-prepare stageComplete'}}
        row = todo_row(id='e1', waiting='', unread=0, replies=[], link={'label': 'PROJ-1451', 'url': 'u'})
        self.assertEqual(todo_actions([(row, 'PROJ-1451')], queue),
                         [['wait', 'e1', "공장 대기 — stg-approval: 승인·보류는 '[스테이징 승인]' 항목에 답해 주세요 · stg-prepare stageComplete"]])

    def test_main_mirrors_queue_and_todo_failure_keeps_jira_scan(self):
        row = dict(todo_row(), waiting='', unread=0)
        cid = todo_reply_id(row, row['replies'][0])
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            (base / 'state.json').write_text(json.dumps({'followup': {'handled': {cid: {'key': 'PROJ-1370'}}}}))
            (base / 'followup-queue.json').write_text(json.dumps({'PROJ-1370': {'waiting': 'human-risk', 'note': 'stg 브라우저 제어권'}}))
            calls = []
            def todo(args):
                calls.append(args)
                return [row] if args[0] == 'list' else None
            with contextlib.redirect_stdout(io.StringIO()):
                scan.main([], runner=lambda argv: [], todo=todo, workspace=base, base=base)
            output = json.loads((base / 'followup-scan.json').read_text())
            self.assertEqual(calls, [['list', '--json'], ['wait', '2e45ab', '공장 대기 — human-risk: stg 브라우저 제어권']])
            self.assertEqual(output['todoSynced'], [['wait', '2e45ab']])
            def broken(args):
                raise RuntimeError('node missing')
            with contextlib.redirect_stdout(io.StringIO()):
                scan.main([], runner=lambda argv: [], todo=broken, workspace=base, base=base)
            output = json.loads((base / 'followup-scan.json').read_text())
            self.assertEqual(output['errors'], [{'scope': 'todo', 'error': 'node missing'}])


if __name__ == '__main__':
    unittest.main()
