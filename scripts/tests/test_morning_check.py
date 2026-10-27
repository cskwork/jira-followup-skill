"""RUNBOOK morning contracts, fully offline injected Jira/Git/dev-note fixtures."""
import contextlib
from datetime import datetime
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
        self.current = m.ORDER[0]
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
        self.refrow = {'repo': self.repo.name, 'branches': [{'branch': 'fix/' + KEY, 'own': True}], 'dev': True, 'stg': True, 'prd_tags': []}
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
        self.assertEqual(row['proposedNext'], m.ORDER[1])
        self.assertEqual(len([c for c in self.calls if c[0] == 'REST' and c[1].endswith('/transitions')]), 1)

    def test_execute_steps_discovery_readbacks_report_and_preservation(self):
        rc, line = self.run_main(True)
        self.assertEqual(rc, 0)
        self.assertIn('transitions=3', line)
        self.assertEqual(self.current, m.ORDER[3])
        self.assertEqual(self.note_calls, ['2026-10-22'])
        self.assertEqual((self.base / 'done-today').read_text(), '2026-10-22\n')
        state = m.load(self.base / 'state.json', {})
        self.assertEqual(state['lastRunAt'], NOW.isoformat())
        self.assertEqual(state['followup'], self.state['followup'])
        self.assertEqual(state['other'], self.state['other'])
        self.assertEqual(state['issues'][KEY]['status'], m.ORDER[3])
        self.assertTrue((self.base / '2026-10-22.md').exists())
        self.assertEqual(len(list(self.out.glob(KEY + '-readback-*.json'))), 3)

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
                value['repos'][0].update(dev=False, stg=False)
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


if __name__ == '__main__':
    unittest.main()
