import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import spinoff_ticket as sp  # noqa: E402
from followup_scan import ME, select_events  # noqa: E402
from datetime import datetime  # noqa: E402

QA = '6400abc:qa-account'
ISSUE = 'PROJ-1346'


def doc(*parts):
    return {'type': 'doc', 'content': [{'type': 'paragraph', 'content': list(parts)}]}


def text(t):
    return {'type': 'text', 'text': t}


class FakeJira:
    """acli·검사 도구를 흉내 낸다. 쓰기 호출은 writes에 남는다."""

    def __init__(self, status='운영 검수 완료', comment_text='운영계 배포 후 로딩바가 좌우 패널을 가립니다. 수정이 필요합니다.',
                 links=(), linked_created='2026-10-01T10:00:00.000+0900', fail_link=False, todo_sticks=True, owner=True):
        self.status, self.links, self.linked_created = status, list(links), linked_created
        self.fail_link, self.todo_sticks, self.owner = fail_link, todo_sticks, owner
        self.writes, self.new = [], None
        self.new_status, self.new_links = '백로그', []
        self.comments = [{'id': '49500', 'author': {'accountId': QA, 'displayName': '요청자'},
                          'created': '2026-10-15T10:01:00.000+0900', 'body': doc(text(comment_text))}]

    def acli(self, args, parse=True):
        cmd = args[0]
        if cmd == 'view':
            return self.view(args[1], args[3])
        self.writes.append(args)
        if cmd == 'create':
            self.new = 'PROJ-1500'
            return {'key': self.new}
        if cmd == 'link':
            if self.fail_link:
                raise RuntimeError('acli link rc=1')
            self.new_links.append({'type': {'name': 'Relates'}, 'inwardIssue': {'key': args[args.index('--in') + 1]}})
            self.links.append({'type': {'name': 'Relates'}, 'outwardIssue': {'key': self.new}})
            return ''
        if cmd == 'transition':
            if self.todo_sticks:
                self.new_status = args[args.index('--status') + 1]
            return ''
        if cmd == 'comment':
            body = json.loads(Path(args[args.index('--body-file') + 1]).read_text())
            self.comments.append({'id': '49999', 'author': {'accountId': ME}, 'created': '2026-10-26T11:00:00.000+0900',
                                  'body': body})
            return ''
        raise AssertionError(args)

    def view(self, key, fields):
        if key == ISSUE:
            return {'fields': {'summary': '페이지 전환 흰 화면 개선', 'status': {'name': self.status},
                               'comment': {'comments': self.comments}, 'issuelinks': self.links}}
        if key == self.new:
            return {'fields': {'status': {'name': self.new_status}, 'assignee': {'accountId': ME},
                               'issuelinks': self.new_links}}
        return {'fields': {'created': self.linked_created}}

    def tool(self, cmd, stdin=None):
        name = Path(cmd[0] if cmd[0] != 'python3' else cmd[1]).name
        if name == 'jira-daily-guard':
            return (0 if self.owner else 1), ''
        if name == 'md_to_adf.py':
            src, out = Path(cmd[2]), Path(cmd[cmd.index('-o') + 1])
            out.write_text(json.dumps(doc(text(src.read_text()))))
            return 0, ''
        if name == 'check_adf.py':
            return 0, ''
        raise AssertionError(cmd)


class SpinoffTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.md = self.dir / 'desc.md'
        self.md.write_text('## 증상\n- 로딩바가 좌우 패널을 가림\n')

    def tearDown(self):
        self.tmp.cleanup()

    def call(self, jira, *extra):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = sp.main(['--key', ISSUE, '--comment-id', '49500', '--summary', '[운영 후속] 로딩바가 패널을 가림 (PROJ-1346)',
                            '--description-md', str(self.md), '--token', 't1', '--dir', str(self.dir), *extra],
                           jira=jira, now=lambda: '2026-10-26T11:00:00+09:00')
        return code, out.getvalue().strip().splitlines()[-1]

    def state(self):
        return json.loads((self.dir / 'state.json').read_text())['followup']

    def test_issue_report_creates_linked_todo_ticket_and_records_handled(self):
        jira = FakeJira()
        code, line = self.call(jira)
        self.assertEqual(code, 0, line)
        self.assertIn('spinoff=created orig=PROJ-1346 new=PROJ-1500 status=할 일', line)
        self.assertEqual([w[0] for w in jira.writes], ['create', 'link', 'transition', 'comment'])
        create = jira.writes[0]
        self.assertEqual(create[create.index('--type') + 1], '버그')
        self.assertEqual(create[create.index('--assignee') + 1], '@me')
        desc = Path(create[create.index('--description-file') + 1])
        self.assertFalse(desc.exists())  # 임시 폴더는 정리된다
        handled = self.state()['handled']['49500']
        self.assertEqual((handled['action'], handled['newKey']), ('spinoff', 'PROJ-1500'))
        self.assertIn('PROJ-1500', json.dumps(jira.comments[-1]['body'], ensure_ascii=False))

    def test_description_header_links_original_ticket_and_comment(self):
        jira = FakeJira()
        fields = jira.view(ISSUE, '')['fields']
        md = sp.description_md(ISSUE, fields, jira.comments[0], '## 증상\n- x')
        self.assertIn('https://jira.example.com/browse/PROJ-1346)', md)
        self.assertIn('?focusedCommentId=49500', md)
        self.assertIn('요청자 · 2026-10-15 10:01', md)

    def test_a20_1273_author_announced_own_ticket_writes_nothing(self):
        jira = FakeJira(comment_text='운영계 체크 완료하여 완료처리 합니다. 2, 3번 확인 완료 1번은 별도 티켓으로 발행하겠습니다.')
        code, line = self.call(jira)
        self.assertEqual(code, 3)
        self.assertTrue(line.startswith('spinoff=author-will-create'))
        self.assertEqual(jira.writes, [])

    def test_ticket_linked_after_comment_is_reported_not_duplicated(self):
        jira = FakeJira(links=[{'type': {'name': 'Relates'}, 'outwardIssue': {'key': 'PROJ-1490'}}],
                        linked_created='2026-10-16T09:00:00.000+0900')
        code, line = self.call(jira)
        self.assertEqual(code, 4)
        self.assertIn('keys=PROJ-1490', line)
        self.assertEqual(jira.writes, [])

    def test_release_tracker_link_does_not_block(self):
        jira = FakeJira(links=[{'type': {'name': 'Relates'}, 'outwardIssue': {
            'key': 'PROJ-1391', 'fields': {'summary': 'Release_2026/10/19~10/26'}}}],
            linked_created='2026-10-19T09:00:00.000+0900')
        self.assertEqual(self.call(jira)[0], 0)

    def test_older_link_does_not_block(self):
        jira = FakeJira(links=[{'type': {'name': 'Relates'}, 'inwardIssue': {'key': 'PROJ-1391'}}])
        self.assertEqual(self.call(jira)[0], 0)

    def test_not_post_release_status_is_rejected(self):
        jira = FakeJira(status='개발 검수 완료')
        code, line = self.call(jira)
        self.assertEqual(code, 2)
        self.assertIn('not-post-release', line)
        self.assertEqual(jira.writes, [])

    def test_link_failure_resumes_without_second_create(self):
        jira = FakeJira(fail_link=True)
        code, line = self.call(jira)
        self.assertEqual(code, 1)
        self.assertTrue(line.startswith('spinoff=step-failed'))
        self.assertEqual(self.state()['spinoffs']['49500']['newKey'], 'PROJ-1500')
        jira.fail_link = False
        code, line = self.call(jira)
        self.assertEqual(code, 0, line)
        self.assertIn('spinoff=resumed', line)
        # 실패한 링크 시도 1회 + 재실행: 생성은 한 번뿐
        self.assertEqual([w[0] for w in jira.writes], ['create', 'link', 'link', 'transition', 'comment'])
        code, line = self.call(jira)
        self.assertEqual(code, 0)
        self.assertIn('spinoff=exists', line)
        self.assertEqual(len(jira.writes), 5)

    def test_transition_not_reflected_fails_readback(self):
        jira = FakeJira(todo_sticks=False)
        code, line = self.call(jira)
        self.assertEqual(code, 1)
        self.assertIn('problems=status=백로그', line)
        self.assertNotIn('handled', self.state())
        self.assertNotIn('todo', self.state()['spinoffs']['49500']['steps'])

    def test_lost_ownership_writes_nothing(self):
        jira = FakeJira(owner=False)
        code, line = self.call(jira)
        self.assertEqual(code, 5)
        self.assertEqual(jira.writes, [])
        self.assertFalse((self.dir / 'state.json').exists())

    def test_dry_run_writes_nothing(self):
        jira = FakeJira()
        code, line = self.call(jira, '--dry-run')
        self.assertEqual(code, 0)
        self.assertTrue(line.startswith('spinoff=dry-run'))
        self.assertEqual(jira.writes, [])
        self.assertFalse((self.dir / 'state.json').exists())


class ScanPostReleaseTest(unittest.TestCase):
    def test_comment_on_post_release_ticket_carries_flag_and_links(self):
        f = {'summary': 's', 'status': {'name': '운영 검수 완료'}, 'assignee': {'accountId': ME, 'displayName': 'me'},
             'issuelinks': [{'type': {'name': 'Relates'},
                             'outwardIssue': {'key': 'PROJ-1391', 'fields': {'summary': 'Release', 'status': {'name': '진행 중'}}}}],
             'comment': {'comments': [{'id': '49500', 'author': {'accountId': QA, 'displayName': '요청자'},
                                       'created': '2026-10-26T10:00:00.000+0900', 'body': doc(text('운영에서 다시 납니다'))}]}}
        events = select_events(ISSUE, f, datetime.fromisoformat('2026-10-25T00:00:00+09:00'), set())
        self.assertTrue(events[0]['postRelease'])
        self.assertEqual(events[0]['links'], [{'key': 'PROJ-1391', 'summary': 'Release', 'status': '진행 중', 'type': 'Relates'}])

    def test_in_flight_ticket_is_not_post_release(self):
        f = {'summary': 's', 'status': {'name': '스테이징 QA 요청'}, 'assignee': {'accountId': ME},
             'comment': {'comments': [{'id': '1', 'author': {'accountId': QA}, 'created': '2026-10-26T10:00:00.000+0900',
                                       'body': doc(text('확인'))}]}}
        events = select_events(ISSUE, f, datetime.fromisoformat('2026-10-25T00:00:00+09:00'), set())
        self.assertFalse(events[0]['postRelease'])
        self.assertNotIn('links', events[0])


if __name__ == '__main__':
    unittest.main()
