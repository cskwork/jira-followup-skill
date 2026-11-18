"""RUNBOOK morning contracts, fully offline injected Jira/Git/dev-note fixtures."""
import contextlib
from datetime import datetime, timedelta
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import morning_check as m

NOW = datetime.fromisoformat('2026-10-22T08:00:00+09:00')
UPDATED = '2026-10-21T08:00:00+09:00'
KEY = 'PROJ-1389'


def reply(argv, data='', rc=0, stderr=''):
    return subprocess.CompletedProcess(argv, rc, json.dumps(data) if not isinstance(data, str) else data, stderr)


class MorningTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(setattr, m, 'REAUTH_CMD', m.REAUTH_CMD)
        m.REAUTH_CMD = 'acli jira auth login'  # fixture: re-auth is configured
        self.base = Path(self.tmp.name) / 'daily'
        self.root = Path(self.tmp.name) / 'root'
        self.out = Path(self.tmp.name) / 'out'
        self.base.mkdir()
        self.root.mkdir()
        self.repo = self.root / 'core-api'
        (self.repo / '.git').mkdir(parents=True)
        self.calls = []
        # 자동 이동은 QA가 끝난 뒤(스테이징 검수 완료) prd 태그로 운영 단계로 가는 것만 남는다(2026-11-02).
        self.current = m.ORDER[4]
        self.fresh = []
        self.history = []
        self.fetch_rc = 0
        self.write_rc = 0
        self.readback_wrong = False
        self.readback_fail = False
        self.wrote = False
        self.note_calls = []
        self.search_error = None
        self.auth_errors = 0
        self.refrow = {'repo': self.repo.name, 'branches': [{'branch': 'fix/' + KEY, 'own': True}], 'dev': True, 'stg': True, 'prd_tags': ['v261021-1']}
        self.state = {'lastRunAt': '2026-10-21T09:00:00+09:00', 'issues': {KEY: {'status': self.current, 'updated': UPDATED}}, 'followup': {'handled': {'7': {'action': 'posted'}}}, 'other': {'keep': True}}
        m.dump(self.base / 'state.json', self.state)

    def runner(self, argv, timeout):
        self.calls.append(argv)
        if argv[0] == 'bash':
            return reply(argv)
        if argv[0] == 'git':
            cmd = argv[3:]
            if cmd[:1] == ['fetch']:
                return reply(argv, rc=self.fetch_rc)
            if cmd[:2] == ['remote', 'get-url']:
                return reply(argv, 'https://example.invalid/repo')
            if cmd[:1] == ['ls-remote']:
                return reply(argv, '\n'.join('abc\trefs/heads/' + name for name in ('fix/' + KEY, 'dev', 'stg')))
            if cmd[:1] == ['rev-parse']:
                return reply(argv, 'abc')
            if cmd[:1] == ['show-ref']:
                return reply(argv, '', rc=1)
            self.fail('unexpected git ' + str(cmd))
        self.assertEqual(argv[:3], ['acli', 'jira', 'workitem'])
        cmd = argv[3:]
        if cmd[0] == 'search':
            self.assertEqual(cmd[2], m.INFLIGHT_JQL + ' ORDER BY updated DESC')
            if self.auth_errors:
                self.auth_errors -= 1
                return reply(argv, 'unauthorized', rc=1)
            if self.search_error:
                return reply(argv, rc=2)
            return reply(argv, [{'key': KEY, 'fields': {'assignee': {'accountId': 'me'}}}])
        if cmd[0] == 'transition':
            self.wrote = True
            if not self.readback_wrong:
                self.current = cmd[cmd.index('--status') + 1]
            return reply(argv, 'success', self.write_rc)
        self.assertEqual(cmd[0], 'view')
        if self.wrote and self.readback_fail:
            return reply(argv, rc=1)
        fields = {'status': {'name': self.current}, 'updated': UPDATED}
        if 'summary,' in cmd[cmd.index('--fields') + 1]:
            fields.update(summary='공유 화면 오류', comment={'comments': self.fresh, 'total': len(self.fresh)}, subtasks=[])
        return reply(argv, {'fields': fields})

    def rest(self, path):
        self.calls.append(['REST', path])
        if '/changelog?' in path:
            return {'values': self.history, 'total': len(self.history)}
        if path.endswith('/transitions'):
            # The workflow must be rediscovered after each write.
            next_status = m.ORDER[m.ORDER.index(self.current) + 1]
            return {'transitions': [{'to': {'name': next_status}}]}
        self.fail('unexpected rest ' + path)

    def refs(self, key, repos):
        self.assertEqual(key, KEY)
        return {'key': key, 'status': 'carried', 'repos': [dict(self.refrow)]}

    def note(self, date):
        self.note_calls.append(date)
        return reply([], 'devnote=ok item=NOTE-1 updated\n')

    def run_main(self, execute=False, **overrides):
        args = ['--base', str(self.base), '--root', str(self.root), '--out-dir', str(self.out)]
        if execute:
            args.append('--execute')
        if 'judgments' in overrides:
            path = Path(self.tmp.name) / 'judgments.json'
            m.dump(path, overrides.pop('judgments'))
            args += ['--judgments', str(path)]
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            rc = m.main(args, runner=self.runner, rest=overrides.pop('rest', self.rest), refs=overrides.pop('refs', self.refs), note=overrides.pop('note', self.note), now=NOW)
        self.assertFalse(overrides)
        self.assertEqual(len(stdout.getvalue().splitlines()), 1)
        return rc, stdout.getvalue().strip()

    def test_dry_run_no_transition_no_state_done_or_note(self):
        rc, line = self.run_main()
        self.assertEqual(rc, 0)
        self.assertIn('transitions=0', line)
        self.assertIn('devnote=skipped dry-run', line)
        self.assertFalse(self.wrote)
        self.assertEqual(m.load(self.base / 'state.json', {}), self.state)
        self.assertFalse((self.base / 'done-today').exists())
        self.assertFalse(self.note_calls)
        row = m.load(self.out / 'transition-results.json', [])[0]
        self.assertEqual(row['proposedNext'], m.ORDER[5])
        self.assertEqual(len([c for c in self.calls if c[0] == 'REST' and c[1].endswith('/transitions')]), 1)

    def test_execute_steps_discovery_readbacks_report_and_preservation(self):
        rc, line = self.run_main(True)
        self.assertEqual(rc, 0)
        self.assertIn('transitions=2', line)
        self.assertEqual(self.current, m.ORDER[6])
        self.assertEqual(self.note_calls, ['2026-10-22'])
        self.assertEqual((self.base / 'done-today').read_text(), '2026-10-22\n')
        state = m.load(self.base / 'state.json', {})
        self.assertEqual(state['lastRunAt'], NOW.isoformat())
        self.assertEqual(state['followup'], self.state['followup'])
        self.assertEqual(state['other'], self.state['other'])
        self.assertEqual(state['issues'][KEY]['status'], m.ORDER[6])
        self.assertTrue((self.base / '2026-10-22.md').exists())
        self.assertEqual(len(list(self.out.glob(KEY + '-readback-*.json'))), 2)

    def test_failed_write_even_matching_readback_never_success(self):
        self.write_rc = 1
        rc, line = self.run_main(True)
        self.assertEqual(rc, 1)
        self.assertIn('transitions=0', line)
        self.assertFalse((self.base / 'done-today').exists())
        self.assertEqual(m.load(self.base / 'state.json', {}), self.state)
        self.assertEqual(m.load(self.out / 'transition-results.json', [])[0]['steps'][0]['result'], 'FAIL')

    def test_wrong_readback_stops_after_one_write(self):
        self.readback_wrong = True
        self.assertEqual(self.run_main(True)[0], 1)
        self.assertEqual(sum(c[:4] == ['acli', 'jira', 'workitem', 'transition'] for c in self.calls), 1)
        self.assertFalse((self.base / 'done-today').exists())

    def test_failed_readback_is_explicit_no_success_state(self):
        self.readback_fail = True
        self.assertEqual(self.run_main(True)[0], 1)
        row = m.load(self.out / 'transition-results.json', [])[0]
        self.assertTrue(row['stateUncertain'])
        self.assertIn('되읽기 실패', row['reason'])
        self.assertEqual(m.load(self.base / 'state.json', {}), self.state)

    def test_missing_forward_transition_reports_block_and_preserves_status(self):
        def rest(path):
            return {'transitions': [{'to': {'name': m.ORDER[0]}}]} if path.endswith('/transitions') else self.rest(path)
        rc, line = self.run_main(True, rest=rest)
        self.assertEqual(rc, 0)
        self.assertFalse(self.wrote)
        self.assertIn('앞 단계 전이 없음', (self.base / '2026-10-22.md').read_text())
        self.assertTrue((self.base / 'done-today').exists())

    def test_fetch_retry_remote_equal_allows_move(self):
        self.fetch_rc = 1
        self.assertEqual(self.run_main(True)[0], 0)
        self.assertEqual(sum(c[0] == 'git' and c[3] == 'fetch' for c in self.calls), 2)
        self.assertTrue(self.wrote)
        self.assertTrue(any(c[0] == 'git' and c[3] == 'ls-remote' for c in self.calls))

    def test_stale_remote_blocks_move(self):
        self.fetch_rc = 1
        original = self.runner
        def runner(argv, timeout):
            if argv[0] == 'git' and argv[3] == 'ls-remote':
                return reply(argv, 'different\trefs/heads/fix/' + KEY)
            return original(argv, timeout)
        self.runner = runner
        self.assertEqual(self.run_main(True)[0], 1)
        self.assertFalse(self.wrote)
        self.assertIn('낡은 ref로 이동 보류', (self.out / 'report.md').read_text())
        self.assertFalse((self.base / 'done-today').exists())

    def test_fetch_retry_recomputes_target(self):
        self.fetch_rc = 1
        count = 0
        def refs(key, repos):
            nonlocal count
            count += 1
            value = self.refs(key, repos)
            if count > 1:
                value['repos'][0].update(dev=False, stg=False, prd_tags=[])
            return value
        self.assertEqual(self.run_main(True, refs=refs)[0], 0)
        self.assertFalse(self.wrote)

    def test_history_detects_rollback_even_same_snapshot_status(self):
        self.history = [{'created': '2026-10-21T12:00:00+09:00', 'author': {'accountId': 'other', 'displayName': 'QA'}, 'items': [{'field': 'status', 'fromString': m.ORDER[4], 'toString': m.ORDER[0]}]}]
        self.run_main()
        issue = m.load(self.out / 'collection.json', {})['issues'][0]
        self.assertEqual(len(issue['history']), 1)
        self.assertIn('타인 상태 변경', (self.out / 'report.md').read_text())

    def test_newly_observed_is_not_new_assignment(self):
        self.state['issues'] = {}
        m.dump(self.base / 'state.json', self.state)
        self.run_main()
        issue = m.load(self.out / 'collection.json', {})['issues'][0]
        self.assertTrue(issue['newlyObserved'])
        self.assertFalse(issue['newlyAssigned'])

    def test_assignment_history_uses_id(self):
        self.history = [{'created': '2026-10-21T12:00:00+09:00', 'author': {'accountId': 'other'}, 'items': [{'field': 'assignee', 'to': 'me', 'toString': 'Me'}]}]
        self.run_main()
        self.assertTrue(m.load(self.out / 'collection.json', {})['issues'][0]['newlyAssigned'])

    def test_unclassified_comment_explicit_not_invented_and_no_done(self):
        self.fresh = [{'id': '42', 'created': '2026-10-21T12:00:00+09:00', 'author': {'accountId': 'other'}, 'body': {'type': 'text', 'text': '확인 요청'}}]
        self.assertEqual(self.run_main(True)[0], 1)
        self.assertIn('모델 판정 대기', (self.out / 'report.md').read_text())
        self.assertFalse((self.base / 'done-today').exists())
        self.assertFalse(self.note_calls)

    def test_supplied_judgment_draft_and_link_removal(self):
        self.fresh = [{'id': '42', 'created': '2026-10-21T12:00:00+09:00', 'author': {'accountId': 'other'}, 'body': {'type': 'text', 'text': '확인 요청'}}]
        judgments = {'comments': {'42': {'key': KEY, 'classification': '답변 필요', 'summary': '배포 문의', 'draft': '[PROJ-1389](https://jira.example.com/browse/PROJ-1389) 확인했습니다.'}}}
        self.assertEqual(self.run_main(True, judgments=judgments)[0], 0)
        report = (self.base / '2026-10-22.md').read_text()
        self.assertIn('답변 필요', report)
        self.assertNotIn('jira.example.com', report)
        self.assertFalse(any(c[:4] == ['acli', 'jira', 'workitem', 'comment'] for c in self.calls))

    def test_invalid_judgment_before_any_transition(self):
        self.fresh = [{'id': '42', 'created': '2026-10-21T12:00:00+09:00', 'author': {'accountId': 'other'}, 'body': {}}]
        self.assertEqual(self.run_main(True, judgments={'comments': {'42': {'key': 'PROJ-1', 'classification': '정보 공유'}}})[0], 1)
        self.assertFalse(self.wrote)

    def test_note_session_expiry_is_skipped_not_blocked(self):
        def note(_date):
            return reply([], 'devnote=skipped 노트 보드 세션 만료: login\n')
        rc, line = self.run_main(True, note=note)
        self.assertEqual(rc, 0, line)
        self.assertIn('devnote=skipped', line)

    def test_note_blocked_still_done_local_valid_and_no_retry(self):
        def note(date):
            self.note_calls.append(date)
            return reply([], 'devnote=BLOCKED 로그인 필요', rc=3)
        rc, line = self.run_main(True, note=note)
        self.assertEqual(rc, 1)
        self.assertTrue((self.base / 'done-today').exists())
        self.assertIn('devnote=BLOCKED 로그인 필요', line)
        self.assertEqual(len(self.note_calls), 1)

    def test_note_exception_still_done_no_retry(self):
        def note(date):
            self.note_calls.append(date)
            raise subprocess.TimeoutExpired('note', 300)
        rc, line = self.run_main(True, note=note)
        self.assertEqual(rc, 1)
        self.assertTrue((self.base / 'done-today').exists())
        self.assertIn('devnote=BLOCKED dev_note_sync 실패: TimeoutExpired', line)
        self.assertEqual(len(self.note_calls), 1)

    def test_auth_reauth_once_then_failure_no_done(self):
        self.auth_errors = 2
        rc, line = self.run_main(True)
        self.assertEqual(rc, 1)
        self.assertIn('BLOCKED: acli 인증', line)
        self.assertEqual(sum(c[0] == 'bash' for c in self.calls), 1)
        self.assertFalse((self.base / 'done-today').exists())

    def test_auth_reauth_recovery(self):
        self.auth_errors = 1
        self.assertEqual(self.run_main()[0], 0)
        self.assertEqual(sum(c[0] == 'bash' for c in self.calls), 1)

    def test_history_failure_no_advance_watermark(self):
        def rest(path):
            if '/changelog?' in path:
                raise OSError('not retained')
            return self.rest(path)
        self.assertEqual(self.run_main(True, rest=rest)[0], 1)
        self.assertEqual(m.load(self.base / 'state.json', {}), self.state)
        self.assertFalse((self.base / 'done-today').exists())

    def test_originless_repo_excluded(self):
        other = self.root / 'local-tool'
        (other / '.git').mkdir(parents=True)
        original = self.runner
        def runner(argv, timeout):
            if argv[0] == 'git' and argv[2] == str(other):
                return reply(argv, rc=2)
            return original(argv, timeout)
        self.runner = runner
        self.assertEqual(self.run_main()[0], 0)
        self.assertEqual(m.load(self.out / 'fetch.json', {})['excludedNoOrigin'], ['local-tool'])

    def test_state_save_reloads_latest_nonmorning_keys(self):
        original = self.note
        def refs(key, repos):
            state = m.load(self.base / 'state.json', {})
            state['followup']['handled']['new'] = {'action': 'human'}
            m.dump(self.base / 'state.json', state)
            return self.refs(key, repos)
        self.assertEqual(self.run_main(True, refs=refs)[0], 0)
        self.assertIn('new', m.load(self.base / 'state.json', {})['followup']['handled'])

    def test_backlog_never_auto_moved(self):
        self.current = '백로그'
        self.assertEqual(self.run_main(True)[0], 0)
        self.assertFalse(self.wrote)

    def test_dev_reflected_in_progress_is_not_auto_moved_to_dev_qa(self):
        # 2026-11-02: dev 반영 Git ref만으로 '진행 중' → '개발 QA 요청'으로 옮겼다(QA·결과 댓글 없음).
        self.current = m.ORDER[0]
        self.refrow.update(stg=False, prd_tags=[])
        self.assertEqual(self.run_main(True)[0], 0)
        self.assertFalse(self.wrote)
        self.assertFalse(any(c[0] == 'REST' and c[1].endswith('/transitions') for c in self.calls))
        row = m.load(self.out / 'transition-results.json', [])[0]
        self.assertEqual((row['before'], row['after'], row['steps']), (m.ORDER[0], m.ORDER[0], []))
        self.assertIn(m.QA_HOLD, row['reason'])
        self.assertIn(m.QA_HOLD, (self.base / '2026-10-22.md').read_text())

    def test_git_reflection_never_moves_into_or_through_qa_states(self):
        # stg·prd 반영이어도 QA 단계('개발 QA 요청'~'스테이징 검수 완료')로 들어가거나 경유하지 않는다.
        cases = [(m.ORDER[0], True, []), (m.ORDER[1], True, []), (m.ORDER[2], True, []),
                 (m.ORDER[0], True, ['v1']), (m.ORDER[3], True, ['v1'])]
        for current, stg, tags in cases:
            with self.subTest(current=current, stg=stg, tags=tags):
                self.current, self.wrote, self.calls = current, False, []
                self.refrow.update(stg=stg, prd_tags=tags)
                self.assertEqual(self.run_main(True)[0], 0)
                self.assertFalse(self.wrote)
                self.assertIn(m.QA_HOLD, m.load(self.out / 'transition-results.json', [])[0]['reason'])

    def test_stg_done_with_prd_tag_moves_to_release_and_deploy_done(self):
        self.current = m.ORDER[4]
        self.assertEqual(self.run_main(True)[0], 0)
        moved = [c[c.index('--status') + 1] for c in self.calls if c[:4] == ['acli', 'jira', 'workitem', 'transition']]
        self.assertEqual(moved, [m.ORDER[5], m.ORDER[6]])
        self.assertEqual(m.load(self.out / 'transition-results.json', [])[0]['reason'], 'PASS: 상태 되읽기 확인')

    def test_status_regressed_before_write_is_not_moved(self):
        # 계획 뒤 다른 사람이 QA 전 단계로 되돌리면 쓰기 직전 재조회에서 멈춘다.
        original = self.runner
        def runner(argv, timeout):
            if argv[3:4] == ['view'] and argv[6] == 'status,updated':
                self.current = m.ORDER[3]
            return original(argv, timeout)
        self.runner = runner
        self.assertEqual(self.run_main(True)[0], 0)
        self.assertFalse(self.wrote)
        self.assertIn(m.QA_HOLD, m.load(self.out / 'transition-results.json', [])[0]['reason'])

    def test_incomplete_comment_page_fails_explicitly(self):
        morning = m.Morning(self.base, self.root, self.out, rest=lambda path: {'comments': [], 'total': 1})
        with self.assertRaisesRegex(ValueError, '불완전'):
            morning.page(KEY, 'comment')

    def test_business_days_and_fallback_friday(self):
        monday = datetime.fromisoformat('2026-10-19T08:00:00+09:00')
        self.assertEqual(m.previous_business_day(monday), '2026-10-16T09:00:00+09:00')
        self.assertEqual(m.business_days('2026-10-09T09:00:00+09:00', NOW), 9)

    def test_default_inspector_own_tip_and_carried_repositories_both_retained(self):
        second = self.root / 'content-web'
        second.mkdir()
        sha = 'a' * 40
        calls = []
        def runner(argv, timeout):
            calls.append(argv)
            if argv[0] == sys.executable:
                return reply(argv, {'key': KEY, 'status': 'carried', 'repos': [
                    {'repo': self.repo.name, 'branches': [], 'dev': True, 'stg': True, 'prd': True, 'prd_tags': ['v1', 'v2']},
                    {'repo': second.name, 'branches': [{'branch': 'fix/PROJ-1', 'own': False}], 'dev': True, 'stg': False, 'prd_tags': []}]})
            cmd = argv[3:]
            if argv[2] == str(second):
                return reply(argv, rc=1)
            if cmd[0] == 'rev-parse':
                return reply(argv, sha)
            if cmd[0] == 'tag':
                return reply(argv, 'v2\n')
            if cmd[0] == 'merge-base':
                return reply(argv, rc=0 if cmd[-1].endswith(('dev', 'main')) else 1)
            self.fail(str(argv))
        morning = m.Morning(self.base, self.root, self.out, runner=runner)
        value = morning.inspect_refs(KEY, [self.repo, second])
        self.assertEqual(len(value['repos']), 2)
        own, carried = value['repos']
        self.assertEqual(own['tip'], sha)
        self.assertEqual(own['prd_tags'], ['v2'])
        self.assertFalse(own['stg'])
        self.assertEqual(carried['repo'], second.name)
        self.assertTrue((self.out / (KEY + '-own-refs.json')).exists())

    def test_empty_repo_set_does_not_invoke_default_root_scanner(self):
        morning = m.Morning(self.base, self.root, self.out, runner=lambda *args, **kwargs: self.fail('must not execute'))
        self.assertEqual(morning.inspect_refs(KEY, [])['repos'], [])

    def inspect_tip(self, marker=None, *, tip_envs=('dev', 'stg', 'prd'), tags=('v-side',), production_tags=()):
        sha = 'a' * 40
        def runner(argv, timeout):
            if argv[0] == sys.executable:
                return reply(argv, {'key': KEY, 'status': 'none', 'repos': [marker] if marker else []})
            cmd = argv[3:]
            if cmd[0] == 'rev-parse':
                return reply(argv, sha)
            if cmd[0] == 'tag':
                return reply(argv, '\n'.join(tags))
            if cmd[0] == 'merge-base':
                if cmd[-2].startswith('refs/tags/'):
                    return reply(argv, rc=0 if cmd[-2].removeprefix('refs/tags/') in production_tags else 1)
                return reply(argv, rc=0 if {'main': 'prd'}.get(env := cmd[-1].removeprefix('origin/'), env) in tip_envs else 1)
            self.fail(str(argv))
        morning = m.Morning(self.base, self.root, self.out, runner=runner)
        return morning.inspect_refs(KEY, [self.repo])['repos'][0]

    def test_side_branch_tag_above_production_tip_is_not_production_tag(self):
        # tip is on prd; v-side adds a side commit above tip, outside prd history.
        row = self.inspect_tip()
        self.assertTrue(row['prd'])
        self.assertEqual(row['prd_tags'], [])
        self.assertEqual(row['prd_state'], 'untagged')

    def test_new_own_tip_cannot_retain_old_markers_tagged_production_state(self):
        marker = {'repo': self.repo.name, 'branches': [], 'commits': ['b' * 9],
                  'dev': True, 'stg': True, 'prd': True, 'prd_state': 'tagged', 'prd_tags': ['v-old']}
        row = self.inspect_tip(marker, tip_envs=('dev',), tags=('v-side',))
        self.assertFalse(row['prd'])
        self.assertFalse(row['stg'])
        self.assertEqual(row['prd_tags'], [])
        self.assertEqual(row['prd_state'], 'not-in-prd')

    def test_own_tip_does_not_drop_unpromoted_markers_on_carried_branch(self):
        marker = {'repo': self.repo.name, 'branches': [{'branch': 'fix/PROJ-1', 'own': False}],
                  'commits': ['b' * 9], 'dev': False, 'stg': False, 'prd': False,
                  'prd_state': 'not-in-prd', 'prd_tags': []}
        row = self.inspect_tip(marker, tags=('v-release',), production_tags=('v-release',))
        self.assertFalse(row['dev'])
        self.assertFalse(row['stg'])
        self.assertFalse(row['prd'])
        self.assertEqual(row['prd_tags'], [])
        self.assertEqual(row['prd_state'], 'not-in-prd')
        self.assertTrue(any(b['branch'] == 'fix/PROJ-1' for b in row['branches']))

    def test_production_tag_must_cover_both_tip_and_markers(self):
        marker = {'repo': self.repo.name, 'branches': [], 'commits': ['b' * 9],
                  'dev': True, 'stg': True, 'prd': True, 'prd_state': 'tagged', 'prd_tags': ['v-marker', 'v-both']}
        row = self.inspect_tip(marker, tags=('v-tip', 'v-both'), production_tags=('v-tip', 'v-both'))
        self.assertTrue(row['prd'])
        self.assertEqual(row['prd_tags'], ['v-both'])
        self.assertEqual(row['prd_state'], 'tagged')


    def test_teams_never_successful_is_counted_and_prioritized_in_morning_report(self):
        m.dump(self.base / 'teams-scan.json', {
            'status': 'unavailable', 'monitoring': {'lastSuccessAt': None}, 'error': 'RUN_CONNECT_FIRST'})
        rc, line = self.run_main()
        self.assertEqual(rc, 0)
        self.assertIn('needs_action=1', line)
        self.assertEqual(m.load(self.out / 'summary.json', {})['needs_action'], 1)
        report = (self.out / 'report.md').read_text()
        priority = next(row for row in report.splitlines() if row.startswith('| 오늘 먼저 할 일 |'))
        self.assertIn('Teams', priority)
        self.assertIn('RUN_CONNECT_FIRST', priority)
        self.assertIn('<채팅 CLI 스킬> connect', priority)
        self.assertIn('| 조치 필요 수 | 1건 |', report)

    def test_teams_recent_failure_and_healthy_monitoring_do_not_add_actions(self):
        for status, success in (('unavailable', NOW - timedelta(hours=24)),
                                ('ok', NOW - timedelta(days=2))):
            with self.subTest(status=status):
                m.dump(self.base / 'teams-scan.json', {
                    'status': status, 'monitoring': {'lastSuccessAt': success.isoformat()}, 'error': 'TEAMS_READ_FAILED'})
                rc, line = self.run_main()
                self.assertEqual(rc, 0)
                self.assertIn('needs_action=0', line)
                self.assertNotIn('<채팅 CLI 스킬> connect', (self.out / 'report.md').read_text())

    def test_teams_long_failure_is_counted_and_malformed_scan_blocks_execute(self):
        m.dump(self.base / 'teams-scan.json', {
            'status': 'unavailable', 'monitoring': {'lastSuccessAt': (NOW - timedelta(hours=24, microseconds=1)).isoformat()},
            'error': 'BROWSER_UNAVAILABLE_OR_CONTROL_REQUIRED'})
        rc, line = self.run_main()
        self.assertEqual(rc, 0)
        self.assertIn('needs_action=1', line)
        self.assertIn('BROWSER_UNAVAILABLE_OR_CONTROL_REQUIRED', (self.out / 'report.md').read_text())
        m.dump(self.base / 'teams-scan.json', [])
        rc, line = self.run_main(True)
        self.assertEqual(rc, 1)
        self.assertIn('BLOCKED', line)
        self.assertFalse(self.wrote)
        self.assertFalse((self.base / 'done-today').exists())
        self.assertEqual(m.load(self.base / 'state.json', {}), self.state)


class TeamsMonitoringTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.morning = m.Morning(self.base, self.base, self.base / 'out',
                                 runner=lambda *args, **kwargs: self.fail('no live calls'))

    def scan_state(self, status='unavailable', success=None, **extra):
        m.dump(self.base / 'teams-scan.json', {
            'status': status, 'scannedAt': NOW.isoformat(), 'monitoring': {'lastSuccessAt': success},
            'error': 'RUN_CONNECT_FIRST', **extra})

    def test_never_successful_monitoring_requires_reconnect_with_reason(self):
        self.scan_state()
        action = self.morning.teams_action(NOW)
        self.assertIn('Teams', action)
        self.assertIn('RUN_CONNECT_FIRST', action)
        self.assertIn('<채팅 CLI 스킬> connect', action)
        self.assertIn('성공 이력 없음', action)

    def test_only_current_unavailability_over_24_hours_requires_action(self):
        for age, warning in ((timedelta(hours=23, minutes=59), False),
                             (timedelta(hours=24), False),
                             (timedelta(hours=24, microseconds=1), True)):
            with self.subTest(age=age):
                self.scan_state(success=(NOW - age).isoformat())
                action = self.morning.teams_action(NOW)
                self.assertEqual(action is not None, warning)
                if warning:
                    self.assertIn('RUN_CONNECT_FIRST', action)
                    self.assertIn('<채팅 CLI 스킬> connect', action)

    def test_healthy_scan_and_recovery_have_no_warning(self):
        self.scan_state(success=(NOW - timedelta(days=2)).isoformat())
        self.assertIsNotNone(self.morning.teams_action(NOW))
        self.scan_state(status='ok', success=NOW.isoformat())
        self.assertIsNone(self.morning.teams_action(NOW))
        self.scan_state(status='ok')
        self.assertIsNone(self.morning.teams_action(NOW))

    def test_old_scan_can_use_persisted_success_but_not_message_mark(self):
        from teams_room import CHAT_ID
        m.dump(self.base / 'teams-scan.json', {'status': 'unavailable', 'error': 'TEAMS_READ_FAILED'})
        room = {'mark': {'id': '1', 'time': NOW.isoformat()}, 'monitoring': {'lastSuccessAt': NOW.isoformat()}}
        m.dump(self.base / 'teams-room.json', {CHAT_ID: room})
        self.assertIsNone(self.morning.teams_action(NOW))
        self.scan_state(success=None)
        self.assertIn('성공 이력 없음', self.morning.teams_action(NOW))
        m.dump(self.base / 'teams-scan.json', {'status': 'unavailable', 'error': 'TEAMS_READ_FAILED'})
        del room['monitoring']
        m.dump(self.base / 'teams-room.json', {CHAT_ID: room})
        self.assertIn('성공 이력 없음', self.morning.teams_action(NOW))

    def test_no_scan_does_not_claim_monitoring_failed(self):
        self.assertIsNone(self.morning.teams_action(NOW))

    def test_missing_scan_explicit_guard_failure_without_success_requires_reconnect(self):
        for guard in ({'teams': {'status': 'unavailable', 'error': 'TEAMS_SCAN_FAILED'}},
                      {'reasons': ['teams-unavailable:TEAMS_CLI_TIMEOUT']}):
            with self.subTest(guard=guard):
                m.dump(self.base / 'guard-last.json', guard)
                action = self.morning.teams_action(NOW)
                self.assertIsNotNone(action)
                self.assertIn('성공 이력 없음', action)
                self.assertIn('<채팅 CLI 스킬> connect', action)
                self.assertIn('TEAMS_SCAN_FAILED' if 'teams' in guard else 'TEAMS_CLI_TIMEOUT', action)

    def test_missing_scan_guard_failure_uses_persisted_success_with_strict_24h_gate(self):
        m.dump(self.base / 'guard-last.json', {
            'teams': {'status': 'unavailable', 'error': 'TEAMS_SCAN_FAILED'}})
        for age, warning in ((timedelta(hours=23, minutes=59), False),
                             (timedelta(hours=24), False),
                             (timedelta(hours=24, microseconds=1), True)):
            with self.subTest(age=age):
                m.dump(self.base / 'teams-room.json', {
                    m.CHAT_ID: {'monitoring': {'lastSuccessAt': (NOW - age).isoformat()}}})
                action = self.morning.teams_action(NOW)
                self.assertEqual(action is not None, warning)
                if warning:
                    self.assertIn('TEAMS_SCAN_FAILED', action)

    def test_guard_without_failure_is_quiet_and_existing_scan_remains_authoritative(self):
        m.dump(self.base / 'teams-room.json', {
            m.CHAT_ID: {'monitoring': {'lastSuccessAt': (NOW - timedelta(days=2)).isoformat()}}})
        for guard in ({}, {'reasons': ['no-events']}, {'teams': {'status': 'ok'}}):
            with self.subTest(guard=guard):
                m.dump(self.base / 'guard-last.json', guard)
                self.assertIsNone(self.morning.teams_action(NOW))
        m.dump(self.base / 'guard-last.json', {
            'teams': {'status': 'unavailable', 'error': 'TEAMS_SCAN_FAILED'}})
        self.scan_state(status='ok', success=NOW.isoformat())
        self.assertIsNone(self.morning.teams_action(NOW))

    def test_missing_reason_is_explicit_and_malformed_states_fail(self):
        self.scan_state(error=None)
        self.assertIn('TEAMS_READ_FAILED', self.morning.teams_action(NOW))
        for state in ([], {'status': 'unknown'},
                      {'status': 'unavailable', 'monitoring': {'lastSuccessAt': 'bad'}},
                      {'status': 'unavailable', 'monitoring': {'lastSuccessAt': '2026-10-21T08:00:00'}},
                      {'status': 'unavailable', 'monitoring': {'lastSuccessAt': 1}}):
            with self.subTest(state=state):
                m.dump(self.base / 'teams-scan.json', state)
                with self.assertRaises(ValueError):
                    self.morning.teams_action(NOW)


if __name__ == '__main__':
    unittest.main()
