"""FIX1 회귀: 임시 상태와 주입한 명령으로만 가드 경계를 검증한다."""
from contextlib import redirect_stdout
from datetime import datetime
import fcntl
import io
import json
import subprocess
import time
from unittest.mock import Mock, patch

from test_run_lock import LivenessFixture, LOCK


class Fix1RunLockTests(LivenessFixture):
    def lease(self, key='PROJ-1', token='ticket', **extra):
        data = dict(key=key, token=token, parent='global', state='launching',
                    startedAt=time.time(), deadline=time.time() + 9900, orchestratorId=None)
        data.update(extra)
        LOCK.atomic_json(self.state / 'tickets' / (key + '.json'), data)
        LOCK.run_path(self.state, token).mkdir(parents=True, exist_ok=True)
        return data

    def worker(self):
        return LOCK.main(['worker', '--dir', str(self.state), '--key', 'PROJ-1', '--token', 'ticket'])

    def test_ticket_worker_skips_finished_parent_event_but_blocks_running_parent(self):
        self.lock('global')
        self.configure(agents=[self.row('event', 'global', status='idle')])
        token = LOCK.lease_acquire(self.state, 'global', 'PROJ-1')
        # 실제 acquire 토큰을 사용해 상위 이벤트 -> 임대 -> worker 순서를 재현한다.
        args = ['worker', '--dir', str(self.state), '--key', 'PROJ-1', '--token', token]
        self.assertEqual(LOCK.main(args), 0)
        self.configure(agents=[self.row('event', 'global', status='running')])
        self.assertEqual(LOCK.main(args), 3)
        self.configure(ls_fail=True)
        self.assertEqual(LOCK.main(args), 3)

    def test_ticket_worker_keeps_unrelated_lost_run_blocker(self):
        self.lock('global')
        self.lease()
        LOCK.mark(self.state, 'lost-other', 'lost')
        self.configure(agents=[self.row('event', 'global'), self.row('old', 'lost-other')])
        self.assertEqual(self.worker(), 3)

    def test_invalid_ticket_files_are_isolated_alerted_once_and_block_their_keys(self):
        self.lock('global')
        self.lease('PROJ-2', 'valid')
        root = self.state / 'tickets'
        broken = self.lease(state='unknown')
        files = {'PROJ-1.json': json.dumps(broken), 'wrong-name.json': '{}',
                 'PROJ-3.json': '{', 'PROJ-4.json': '{"key":"PROJ-4","token":"bad"}',
                 'PROJ-5.json': json.dumps(dict(broken, key='PROJ-5', state='live', startedAt='bad'))}
        for name, body in files.items():
            (root / name).write_text(body)
        self.configure(agents=[self.row('owner', 'valid', status='running')])
        with patch.dict('sys.modules', decision_inbox=Mock()) as modules:
            notify = modules['decision_inbox'].alert_detached
            for _ in range(2):
                self.assertEqual(LOCK.lease_sweep(self.state), [])
                self.assertEqual([row['key'] for row in LOCK.lease_list(self.state)], ['PROJ-2'])
                self.assertIn('PROJ-1', LOCK.lease_conflicts(self.state, 'PROJ-1'))
                self.assertEqual(LOCK.main(['guard', '--manual', '--dir', str(self.state)]), 0)
            self.assertEqual(notify.call_count, len(files))
        for name, body in files.items():
            self.assertEqual((root / name).read_text(), body)
        journal = ''.join(path.read_text() for path in self.state.glob('followup-*.md'))
        for name in files:
            self.assertEqual(sum('liveness-alert: invalid ' in line and name in line
                                 for line in journal.splitlines()), 1)
        self.assertEqual(LOCK.main(['lease-acquire', '--dir', str(self.state), '--parent', 'global',
                                   '--key', 'PROJ-3']), 1)

    def test_idle_lock_excludes_tick_and_refill_but_manual_still_runs(self):
        (self.state / 'done-today').write_text(datetime.now().date().isoformat())
        for flags in ([], ['--refill']):
            with self.subTest(flags=flags), (self.state / '.idle.lock').open('a') as holder:
                fcntl.flock(holder, fcntl.LOCK_EX)
                result = subprocess.CompletedProcess([], 1, 'run\n', '')
                output = io.StringIO()
                with patch.object(LOCK.subprocess, 'run', return_value=result) as run, redirect_stdout(output):
                    self.assertEqual(LOCK.main(['guard', '--dir', str(self.state), *flags]), 0)
                self.assertIn('jira-daily-guard: skipping duplicate (idle-check running)', output.getvalue())
                self.assertFalse(any('followup_idle.py' in str(call.args[0]) for call in run.call_args_list))
                self.assertFalse((self.state / 'running.json').exists())
        with (self.state / '.idle.lock').open('a') as holder:
            fcntl.flock(holder, fcntl.LOCK_EX)
            with patch.object(LOCK.subprocess, 'run', side_effect=AssertionError('manual scanned')):
                self.assertEqual(LOCK.main(['guard', '--manual', '--dir', str(self.state)]), 0)

    def test_invalid_lease_observation_fields_do_not_abort_sweep(self):
        for field, value in (('queryFailures', 'bad'), ('noLiveSince', 'bad'),
                             ('livenessAlertAt', [])):
            with self.subTest(field=field), patch.dict('sys.modules', decision_inbox=Mock()):
                original = self.lease(**{field: value})
                path = self.state / 'tickets/PROJ-1.json'
                self.configure(ls_fail=True)
                self.assertEqual(LOCK.lease_sweep(self.state), [])
                self.assertEqual(json.loads(path.read_text()), original)
                self.assertEqual(LOCK.lease_list(self.state), [])

    def test_idle_lock_is_held_for_scan_and_released_after_error(self):
        def scan(argv, **kwargs):
            self.assertTrue(argv[1].endswith('followup_idle.py'))
            with (self.state / '.idle.lock').open('a') as contender:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
            raise OSError('scan failed')
        with patch.object(LOCK.subprocess, 'run', side_effect=scan):
            self.assertEqual(LOCK.main(['guard', '--refill', '--dir', str(self.state)]), 2)
        with (self.state / '.idle.lock').open('a') as contender:
            fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_launching_attach_selects_running_ticket_orchestrator_role(self):
        self.lease(startedAt=1000)
        worker = self.row('worker', 'ticket', status='running')
        worker['labels']['role'] = 'worker'
        owner = self.row('owner', 'ticket', status='running')
        owner['labels']['role'] = 'ticket-orchestrator'
        self.configure(agents=[worker, owner])
        self.assertEqual(LOCK.lease_sweep(self.state, now=1600), [])
        data = LOCK.read_lease(self.state, 'PROJ-1')
        self.assertEqual((data['state'], data['orchestratorId']), ('live', 'owner'))
        self.assertTrue(any('role=ticket-orchestrator' in call['args'] for call in self.recorded()))

    def test_launching_worker_alone_never_becomes_orchestrator(self):
        self.lease(startedAt=1000)
        self.configure(agents=[self.row('worker', 'ticket', status='running')])
        self.assertEqual(LOCK.lease_sweep(self.state, now=1600), [])
        data = LOCK.read_lease(self.state, 'PROJ-1')
        self.assertEqual((data['state'], data['orchestratorId']), ('launching', None))
