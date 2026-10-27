"""단계 A 오프라인 계약. 모든 Paseo 조회·실행은 PATH 첫 스텁만 쓴다."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
SPEC = importlib.util.spec_from_file_location("liveness_lock", SCRIPTS / "run_lock.py")
LOCK = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(LOCK)

STUB = '''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
config = json.loads(Path(os.environ['PASEO_FIXTURE']).read_text())
with Path(os.environ['PASEO_CALLS']).open('a') as stream:
    stream.write(json.dumps({'args': args, 'parent': os.environ.get('PASEO_AGENT_ID'),
                             'workspace': os.environ.get('PASEO_WORKSPACE_ID')}) + '\\n')
if args[:2] == ['agent', 'ls']:
    if config.get('ls_fail'):
        sys.exit(1)
    if config.get('ls_invalid'):
        print(config['ls_invalid']); sys.exit(0)
    labels = [args[i + 1] for i, value in enumerate(args) if value == '--label']
    rows = [row for row in config.get('agents', [])
            if all(row.get('labels', {}).get(kv.split('=', 1)[0]) == kv.split('=', 1)[1] for kv in labels)
            and ('-a' in args or not row.get('archived', False))]
    print(json.dumps([{k: v for k, v in row.items() if k not in ('labels', 'archived')} for row in rows]))
elif args[:2] == ['agent', 'inspect']:
    if config.get('inspect_fail'): sys.exit(1)
    print(json.dumps(config.get('inspect', {'Archived': False, 'Status': 'idle',
                                          'ArchivedAt': None, 'UpdatedAt': '', 'ParentAgentId': None})))
elif args[:2] == ['agent', 'logs']:
    print(config.get('logs', 'recovered worker output'))
elif args[:2] == ['agent', 'archive']:
    if args[2] == config.get('archive_fail'): sys.exit(1)
elif args[0] == 'run':
    print(config.get('run_output', '{"status":"done","stage":"build"}'))
    sys.exit(config.get('run_rc', 0))
else:
    sys.exit(9)
'''


class LivenessFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="liveness-stub-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.state = self.root / "state with spaces"
        self.state.mkdir()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.config = self.root / "paseo.json"
        self.calls = self.root / "calls.jsonl"
        self.notification = self.root / "notifications"
        (self.bin / "paseo").write_text(STUB)
        (self.bin / "paseo").chmod(0o755)
        (self.bin / "osascript").write_text('#!/bin/sh\necho alert >> "$NOTIFICATIONS"\n')
        (self.bin / "osascript").chmod(0o755)
        self.settings = {}
        self.configure()
        self.env = dict(os.environ, HOME=str(self.root), JIRA_WAKE_DIR=str(self.state),
                        JIRA_WORKER_CWD=str(self.root), JIRA_WAKE_CWD=str(self.root),
                        PATH=f"{self.bin}:{os.environ['PATH']}", PASEO_FIXTURE=str(self.config),
                        PASEO_CALLS=str(self.calls), NOTIFICATIONS=str(self.notification))
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, self.env).start()

    def configure(self, **kwargs):
        self.settings.update(kwargs)
        self.config.write_text(json.dumps(self.settings))

    def row(self, identity="worker", token="old", status="idle", key="PROJ-1", archived=False):
        labels = {"run": token}
        if key:
            labels['key'] = key
        return dict(id=identity, status=status, labels=labels, archived=archived)

    def lock(self, token="old", **extra):
        data = dict(token=token, startedAt=int(time.time()), mode="tick", liveness="on",
                    orchestratorId="orchestrator")
        data.update(extra)
        LOCK.atomic_json(self.state / "running.json", data)
        (self.state / "running").write_text(str(data['startedAt']))
        (self.state / "runs" / token).mkdir(parents=True, exist_ok=True)
        return data

    def cli(self, script, *args):
        return subprocess.run([sys.executable if script.endswith('.py') else '/bin/bash',
                               str(SCRIPTS / script), *args], env=self.env,
                              capture_output=True, text=True, timeout=15)

    def recorded(self):
        return [json.loads(line) for line in self.calls.read_text().splitlines()] if self.calls.exists() else []

    def busy(self, now=None):
        with LOCK.locked(self.state):
            return LOCK.busy(self.state, now)

    def lost(self, token="old"):
        LOCK.mark(self.state, token, 'lost')

    def idle(self, queue=None):
        from followup_idle import decide
        if queue is not None:
            LOCK.atomic_json(self.state / 'followup-queue.json', queue)
        scan = self.root / 'scan.py'
        scan.write_text("import json, sys\nfrom pathlib import Path\n"
                        "Path(sys.argv[sys.argv.index('--out')+1]).write_text(json.dumps({'events': [], 'errors': []}))\n")
        return decide(self.state, [sys.executable, str(scan)], [sys.executable, '-c', 'raise SystemExit(0)'])


class LegacyLockMigration(LivenessFixture):
    """배포 직전에 시작한 예전 실행(running.json 없음)이 토큰 없이도 끝낼 수 있어야 한다."""

    def test_tokenless_release_clears_legacy_epoch_lock(self):
        (self.state / 'running').write_text(str(int(time.time())))
        response = self.cli('jira-daily-guard', '--release')
        self.assertEqual(response.returncode, 0, response.stderr)
        self.assertFalse((self.state / 'running').exists())

    def test_tokenless_worker_allowed_only_for_legacy_lock(self):
        (self.state / 'running').write_text(str(int(time.time())))
        legacy = self.cli('run_lock.py', 'worker', '--dir', str(self.state), '--key', 'PROJ-1')
        self.assertEqual(legacy.returncode, 0, legacy.stderr)
        self.lock()
        modern = self.cli('run_lock.py', 'worker', '--dir', str(self.state), '--key', 'PROJ-1')
        self.assertEqual(modern.returncode, 3)


class InvalidLockFile(LivenessFixture):
    def test_invalid_running_json_falls_back_to_epoch_and_alerts_once(self):
        (self.state / 'running.json').write_text('{broken')
        (self.state / 'running').write_text(str(int(time.time())))
        self.assertTrue(self.busy())
        self.assertTrue(self.busy())
        journal = next(self.state.glob('followup-*.md')).read_text()
        self.assertEqual(journal.count('invalid running.json'), 1)


class ReleaseBySelf(LivenessFixture):
    def test_release_by_running_owner_writes_ended(self):
        """2026-10-23 11:33 실행: 해제 시점에 오케스트레이터 자신만 running이어서 lost로 잘못 남았다."""
        self.lock()
        self.configure(agents=[self.row('orchestrator', status='running', key=None),
                               self.row('worker', status='idle')])
        response = self.cli('jira-daily-guard', '--release', '--token', 'old')
        self.assertEqual(response.returncode, 0, response.stderr)
        self.assertTrue((self.state / 'runs/old/ended').exists())
        self.assertFalse((self.state / 'runs/old/lost').exists())

    def test_release_with_running_worker_still_writes_lost(self):
        self.lock()
        self.configure(agents=[self.row('orchestrator', status='running', key=None),
                               self.row('worker', status='running')])
        self.assertEqual(self.cli('jira-daily-guard', '--release', '--token', 'old').returncode, 0)
        self.assertTrue((self.state / 'runs/old/lost').exists())


class LivenessChecks(LivenessFixture):
    def test_A1_release_requires_matching_token_and_owns(self):
        self.lock()
        before = (self.state / 'running.json').read_bytes()
        for args, code in ((['--release'], 2), (['--release', '--token', 'wrong'], 1),
                           (['--owns', 'wrong'], 1), (['--owns', 'old'], 0)):
            response = self.cli('jira-daily-guard', *args)
            self.assertEqual(response.returncode, code, response.stderr)
            self.assertEqual((self.state / 'running.json').read_bytes(), before)
        self.assertEqual(self.cli('jira-daily-guard', '--release', '--token', 'old').returncode, 0)
        self.assertFalse((self.state / 'running').exists())
        self.assertFalse((self.state / 'running.json').exists())
        self.assertTrue((self.state / 'runs/old/ended').exists())

    def test_A2_closed_or_archived_owner_reclaimed_immediately(self):
        for owner in ({'Archived': False, 'Status': 'closed'}, {'Archived': True, 'Status': 'idle'}):
            self.lock()
            self.configure(agents=[self.row()], inspect=owner)
            self.assertFalse(self.busy())
            self.assertTrue((self.state / 'runs/old/lost').exists())
            self.assertFalse((self.state / 'running.json').exists())
        self.assertEqual(len((self.state / 'lessons.jsonl').read_text().splitlines()), 2)
        self.assertTrue(all(json.loads(line)['kind'] == 'infra_fail'
                            for line in (self.state / 'lessons.jsonl').read_text().splitlines()))

    def test_A2_running_worker_protects_dead_owner_and_clears_noLiveSince(self):
        self.lock(noLiveSince=1)
        self.configure(agents=[self.row(status='running')], inspect={'Archived': True, 'Status': 'closed'})
        self.assertTrue(self.busy())
        self.assertNotIn('noLiveSince', LOCK.read_lock(self.state))
        self.assertFalse(any(row['args'][:2] == ['agent', 'inspect'] for row in self.recorded()))

    def test_A3_idle_owner_needs_25_minutes_without_running(self):
        data = self.lock()
        self.configure(agents=[self.row()])
        now = data['startedAt']
        self.assertTrue(self.busy(now))
        self.assertEqual(LOCK.read_lock(self.state)['noLiveSince'], now)
        self.assertTrue(self.busy(now + 1499))
        self.assertFalse(self.busy(now + 1500))

    def test_A3_query_failure_or_invalid_payload_never_reclaims(self):
        for setting in ({'ls_fail': True}, {'ls_invalid': '{}'}, {'ls_invalid': '[{}]'},
                        {'inspect_fail': True}, {'inspect': {'Status': 'closed'}}):
            with self.subTest(setting=setting):
                self.settings = {}
                self.configure(**setting)
                self.lock(startedAt=1, noLiveSince=1)
                self.assertTrue(self.busy())
                self.assertTrue((self.state / 'running').exists())

    def test_missing_orchestrator_id_has_10_minute_grace_and_running_protection(self):
        self.lock(orchestratorId=None, startedAt=1000)
        self.assertTrue(self.busy(1599))
        self.assertFalse(self.busy(1600))
        self.lock(orchestratorId=None, startedAt=1000)
        self.configure(agents=[self.row(status='running')])
        self.assertTrue(self.busy(9000))

    def test_three_consecutive_failed_queries_alert_once_and_success_resets_count(self):
        self.lock()
        self.configure(ls_fail=True)
        self.assertTrue(self.busy())
        self.assertTrue(self.busy())
        self.assertFalse(self.notification.exists())
        self.assertTrue(self.busy())
        self.assertTrue(self.busy())
        self.assertEqual(LOCK.read_lock(self.state)['queryFailures'], 4)
        self.assertEqual(self.notification.read_text().splitlines(), ['alert'])
        self.configure(ls_fail=False, agents=[self.row(status='running')])
        self.assertTrue(self.busy())
        self.assertEqual(LOCK.read_lock(self.state)['queryFailures'], 0)
        self.assertEqual(sum('liveness-alert' in line for path in self.state.glob('followup-*.md')
                             for line in path.read_text().splitlines()), 1)

    def test_six_hour_lock_alert_does_not_reclaim_running_worker(self):
        self.lock(startedAt=time.time() - 21601)
        self.configure(agents=[self.row(status='running')])
        self.assertTrue(self.busy())
        self.assertTrue(self.busy())
        self.assertEqual(self.notification.read_text().splitlines(), ['alert'])

    def test_A4_archive_only_ended_local_token_nonrunning_agents(self):
        LOCK.mark(self.state, 'finished', 'ended')
        self.lost('lost')
        other = self.root / 'production'
        LOCK.mark(other, 'production', 'ended')
        self.configure(agents=[self.row('done', 'finished'), self.row('closed', 'finished', 'closed'),
            self.row('live', 'finished', 'running'), self.row('lost', 'lost'),
            self.row('prod', 'production'), dict(id='unlabelled', status='idle')])
        response = self.cli('run_lock.py', 'archive', '--dir', str(self.state))
        self.assertEqual(response.returncode, 0, response.stderr)
        self.assertIn('archived=2 archive_failed=0', response.stdout)
        self.assertEqual([row['args'][2] for row in self.recorded()
                          if row['args'][:2] == ['agent', 'archive']], ['done', 'closed'])
        queries = [row['args'] for row in self.recorded() if row['args'][:2] == ['agent', 'ls']]
        self.assertEqual(queries, [['agent', 'ls', '--json', '--label', 'run=finished']])

    def test_A5_lost_reconcile_precedes_key_selection_and_checks_running_again(self):
        self.lost()
        self.configure(agents=[self.row()])
        result, code = self.idle({'PROJ-1': {'waiting': None}})
        self.assertEqual(code, 1)
        self.assertIn('reconcile:old', result['reasons'])
        self.assertEqual(result['blockedKeys'], {'PROJ-1': ['old']})
        self.assertNotIn('queue-runnable:PROJ-1', result['reasons'])
        self.lock('new')
        recovered = self.cli('run_lock.py', 'recover', '--token', 'old', '--current-token', 'new')
        self.assertEqual(recovered.returncode, 0, recovered.stderr)
        self.assertIn('recovered worker output', (self.state / 'runs/old/worker.recovered.txt').read_text())
        self.assertFalse((self.state / 'runs/old/ended').exists())
        self.configure(agents=[self.row(status='running')])
        end = self.cli('run_lock.py', 'reconciled', '--token', 'old', '--current-token', 'new')
        self.assertEqual(end.returncode, 1)
        self.assertFalse((self.state / 'runs/old/ended').exists())
        self.configure(agents=[self.row()])
        self.assertEqual(self.cli('run_lock.py', 'reconciled', '--token', 'old', '--current-token', 'new').returncode, 0)
        self.assertEqual(LOCK.blocked_by(self.state, 'PROJ-1'), [])

    def test_reconcile_only_when_no_running_keeps_key_blocked_without_wake(self):
        self.lost()
        self.configure(agents=[self.row(status='running')])
        result, code = self.idle({'PROJ-1': {'waiting': None}})
        self.assertEqual(code, 0, result)
        self.assertTrue(result['idle'])
        self.assertNotIn('reconcile:old', result['reasons'])
        self.assertEqual(result['blockedKeys'], {'PROJ-1': ['old']})
        self.assertTrue((self.state / 'runs/old/lost').exists())

    def test_archived_agents_do_not_block_key_but_logs_remain_recoverable(self):
        self.lost()
        self.configure(agents=[self.row(archived=True)])
        self.assertEqual(LOCK.blocked_by(self.state, 'PROJ-1'), [])
        self.lock('new')
        self.assertEqual(self.cli('run_lock.py', 'recover', '--token', 'old', '--current-token', 'new').returncode, 0)
        self.assertTrue((self.state / 'runs/old/worker.recovered.txt').exists())

    def test_A6_off_uses_three_hour_epoch_no_token_or_key_blocking(self):
        (self.state / 'liveness').write_text('off\n')
        self.lock(startedAt=1000)
        self.configure(ls_fail=True)
        self.assertTrue(self.busy(11799))
        self.assertFalse(self.busy(11800))
        self.assertEqual(self.recorded(), [])
        self.lost()
        self.assertEqual(LOCK.reconcile_reasons(self.state), [])
        self.assertEqual(LOCK.blocked_by(self.state, 'PROJ-1'), [])
        self.assertEqual(self.cli('jira-daily-guard', '--release').returncode, 0)
        self.assertFalse((self.state / 'running.json').exists())
        brief = self.root / 'brief.md'
        brief.write_text('목표: rollback\n')
        response = self.cli('jira-worker', 'sol', 'unit', str(brief))
        self.assertEqual(response.returncode, 0, response.stderr)
        self.assertIsNone(self.recorded()[-1]['parent'])

    def test_H1_normal_release_then_next_run_same_key_not_blocked(self):
        self.lock()
        self.configure(agents=[self.row()])
        self.assertEqual(self.cli('jira-daily-guard', '--release', '--token', 'old').returncode, 0)
        response = self.cli('jira-daily-guard', '--manual')
        self.assertEqual(response.returncode, 0, response.stderr)
        self.assertIn('mode=morning', response.stdout)
        self.assertNotEqual(LOCK.read_lock(self.state)['token'], 'old')
        result, code = self.idle({'PROJ-1': {'waiting': None}})
        self.assertEqual(code, 1)
        self.assertIn('queue-runnable:PROJ-1', result['reasons'])
        self.assertEqual(result['blockedKeys'], {})

    def test_release_with_running_or_query_failure_marks_lost(self):
        for settings in ({'agents': [self.row(status='running')]}, {'ls_fail': True}):
            self.settings = {}
            self.configure(**settings)
            self.lock()
            self.assertEqual(self.cli('jira-daily-guard', '--release', '--token', 'old').returncode, 0)
            self.assertTrue((self.state / 'runs/old/lost').exists())
            self.assertFalse((self.state / 'runs/old/ended').exists())

    def test_worker_mismatch_exit3_parentless_labels_and_direct_tee(self):
        self.lock('new')
        brief = self.root / 'brief.md'
        brief.write_text('목표: 시험\n')
        for token in ('old', ''):
            response = self.cli('jira-worker', 'sol', 'build', str(brief), *(['--token', token] if token else []))
            self.assertEqual(response.returncode, 3, response.stderr)
        self.assertEqual(self.recorded(), [])
        self.env.update(PASEO_AGENT_ID='parent', PASEO_WORKSPACE_ID='workspace')
        response = self.cli('jira-worker', 'sol', 'build', str(brief), '120m',
                            '--token', 'new', '--key', 'PROJ-1', '--dir', str(self.state))
        self.assertEqual(response.returncode, 0, response.stderr)
        call = self.recorded()[-1]
        self.assertIsNone(call['parent'])
        self.assertIsNone(call['workspace'])
        self.assertIn('run=new', call['args'])
        self.assertIn('key=PROJ-1', call['args'])
        self.assertEqual((self.state / 'runs/new/PROJ-1-build.json').read_text(), response.stdout)

    def test_worker_key_optional_and_failure_propagates_through_tee(self):
        self.lock('new')
        self.configure(run_rc=5)
        brief = self.root / 'brief.md'
        brief.write_text('목표: 아침 점검\n')
        response = self.cli('jira-worker', 'sol', 'morning', str(brief), '--token', 'new')
        self.assertEqual(response.returncode, 5, response.stderr)
        self.assertFalse(any(arg.startswith('key=') for arg in self.recorded()[-1]['args']))
        self.assertTrue((self.state / 'runs/new/no-key-morning.json').exists())

    def test_worker_refuses_key_owned_by_previous_run(self):
        self.lost()
        self.lock('new')
        self.configure(agents=[self.row()])
        brief = self.root / 'brief.md'
        brief.write_text('목표: 시험\n')
        response = self.cli('jira-worker', 'sol', 'build', str(brief), '--token', 'new', '--key', 'PROJ-1')
        self.assertEqual(response.returncode, 3)
        self.assertFalse(any(row['args'][0] == 'run' for row in self.recorded()))

    def test_attach_updates_atomically_and_rejects_stale_owner(self):
        self.lock('new')
        response = self.cli('run_lock.py', 'attach', '--token', 'old', '--id', 'old-orchestrator')
        self.assertEqual(response.returncode, 1)
        with patch.object(LOCK.os, 'replace', wraps=os.replace) as replace:
            data = LOCK.read_lock(self.state)
            data['orchestratorId'] = 'new-orchestrator'
            LOCK.atomic_json(self.state / 'running.json', data)
            self.assertEqual(replace.call_count, 1)
        response = self.cli('run_lock.py', 'attach', '--token', 'new', '--id', 'new-orchestrator')
        self.assertEqual(response.returncode, 0, response.stderr)
        self.assertEqual(LOCK.read_lock(self.state)['orchestratorId'], 'new-orchestrator')

    def test_failed_atomic_update_preserves_original_running_json(self):
        self.lock()
        before = (self.state / 'running.json').read_bytes()
        with patch.object(LOCK.os, 'replace', side_effect=OSError('replace failed')):
            with self.assertRaises(OSError):
                self.busy()
        self.assertEqual((self.state / 'running.json').read_bytes(), before)
        self.assertEqual(list(self.state.glob('.running.json*')), [])

    def test_off_ignores_malformed_running_json_and_force_recovers_it(self):
        self.lock(startedAt=int(time.time()))
        (self.state / 'liveness').write_text('off')
        temporary = self.state / '.broken-lock'
        temporary.write_text('{')
        os.replace(temporary, self.state / 'running.json')
        self.assertTrue(self.busy())
        self.assertEqual(self.recorded(), [])
        self.assertEqual(self.cli('jira-daily-guard', '--release').returncode, 0)
        self.assertFalse((self.state / 'running.json').exists())



class WakeLivenessChecks(LivenessFixture):
    def wake(self, *, missing_mode=False, **env):
        wrapper = self.root / 'guard-wrapper'
        wrapper.write_text("#!/bin/bash\nprintf '%s\\n' \"$@\" >> \"$GUARD_TRACE\"\necho END >> \"$GUARD_TRACE\"\n"
            + ("/bin/bash \"$REAL_GUARD\" \"$@\" | sed '/^mode=/d'\n" if missing_mode
               else 'exec /bin/bash "$REAL_GUARD" "$@"\n'))
        self.env.update(JIRA_WAKE_GUARD=str(wrapper), REAL_GUARD=str(SCRIPTS / 'jira-daily-guard'),
                        GUARD_TRACE=str(self.root / 'guard-trace'))
        self.env.update(env)
        return self.cli('jira-daily-wake', '--now', '--dir', str(self.state))

    def release_args(self):
        calls = (self.root / 'guard-trace').read_text().split('END\n')
        return [call.strip().splitlines() for call in calls if '--release' in call]

    def test_wake_launch_passes_token_label_prompt_and_attaches_owner(self):
        self.configure(run_output='new-orchestrator')
        response = self.wake()
        self.assertEqual(response.returncode, 0, response.stderr)
        data = LOCK.read_lock(self.state)
        self.assertEqual(data['orchestratorId'], 'new-orchestrator')
        call = self.recorded()[-1]
        self.assertIn('run=' + data['token'], call['args'])
        self.assertIn('run_token=' + data['token'], call['args'][-1])
        self.assertIn('--token ' + data['token'], call['args'][-1])
        self.assertIn('--dir', call['args'][-1])

    def test_wake_other_state_directory_cannot_archive_production_tokens(self):
        production = self.root / 'production-state'
        LOCK.mark(production, 'production', 'ended')
        self.configure(agents=[self.row('production-agent', 'production')], run_output='new-orchestrator')
        response = self.wake()
        self.assertEqual(response.returncode, 0, response.stderr)
        self.assertFalse(any(call['args'][:2] == ['agent', 'archive'] for call in self.recorded()))
        self.assertFalse(any(call['args'][:2] == ['agent', 'ls'] for call in self.recorded()))

    def test_all_wake_release_paths_pass_matching_token(self):
        for scenario in ('dry-run', 'launch-failed', 'missing-mode'):
            with self.subTest(scenario=scenario):
                (self.root / 'guard-trace').unlink(missing_ok=True)
                self.settings = {}
                self.configure(run_rc=1 if scenario == 'launch-failed' else 0)
                self.env.pop('JIRA_WAKE_DRY_RUN', None)
                if scenario == 'dry-run':
                    self.env['JIRA_WAKE_DRY_RUN'] = '1'
                response = self.wake(missing_mode=scenario == 'missing-mode')
                self.assertEqual(response.returncode, 0 if scenario == 'dry-run' else 1,
                                 response.stdout + response.stderr)
                calls = self.release_args()
                self.assertEqual(len(calls), 1)
                args = calls[0]
                self.assertIn('--token', args)
                token = args[args.index('--token') + 1]
                self.assertTrue(LOCK.valid_token(token))
                self.assertTrue((self.state / 'runs' / token / 'ended').exists())
                self.assertFalse((self.state / 'running.json').exists())


class LivenessDrill(LivenessFixture):
    def test_dead_owner_reclaim_reconcile_end_and_archive_only_that_token(self):
        self.lock('dead')
        self.configure(agents=[self.row('dead-worker', 'dead'), self.row('other-worker', 'other')],
                       inspect={'Archived': True, 'Status': 'closed'})
        response = self.cli('jira-daily-guard', '--manual')
        self.assertEqual(response.returncode, 0, response.stderr)
        current = LOCK.read_lock(self.state)['token']
        self.assertNotEqual(current, 'dead')
        self.assertTrue((self.state / 'runs/dead/lost').exists())
        result, code = self.idle({'PROJ-1': {'waiting': None, 'slug': 'example', 'stage': 'plan'}})
        self.assertEqual(code, 1)
        self.assertIn('reconcile:dead', result['reasons'])
        self.assertEqual(result['blockedKeys'], {'PROJ-1': ['dead']})
        response = self.cli('run_lock.py', 'recover', '--token', 'dead', '--current-token', current)
        self.assertEqual(response.returncode, 0, response.stderr)
        # 실제 운영에서는 오케스트레이터가 slug logs의 검증 결과로 큐를 대조한다.
        evidence = self.state / 'logs/example/check-build.json'
        LOCK.atomic_json(evidence, {'status': 'PASS', 'stage': 'build'})
        stage = json.loads(evidence.read_text())['stage']
        LOCK.atomic_json(self.state / 'followup-queue.json', {'PROJ-1': {'waiting': None, 'stage': stage}})
        response = self.cli('run_lock.py', 'reconciled', '--token', 'dead', '--current-token', current)
        self.assertEqual(response.returncode, 0, response.stderr)
        self.assertTrue((self.state / 'runs/dead/ended').exists())
        self.assertEqual(LOCK.blocked_by(self.state, 'PROJ-1'), [])
        response = self.cli('run_lock.py', 'archive')
        self.assertEqual(response.returncode, 0, response.stderr)
        archived = [row['args'][2] for row in self.recorded() if row['args'][:2] == ['agent', 'archive']]
        self.assertEqual(archived, ['dead-worker'])
        if os.environ.get('LIVENESS_DRILL_OUTPUT') == '1':
            print('drill=PASS dead-owner -> immediate-reclaim -> reconcile:dead -> logs+verifier+queue -> ended -> archive=dead-worker only; real_paseo_calls=0')


if __name__ == '__main__':
    unittest.main()
