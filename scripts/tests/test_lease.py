"""티켓 임대 계약. 임시 상태와 PATH Paseo 스텁만 사용한다."""
import json
import subprocess
import sys
import time
from unittest.mock import patch

from test_run_lock import LivenessFixture, LOCK, SCRIPTS


class TicketLeases(LivenessFixture):
    def setUp(self):
        super().setUp()
        self.decisions = []

        def open_decision(directory, path):
            import decision_page
            spec, name = decision_page.load_spec('spec', str(path), directory)
            self.decisions.append(spec)
            return decision_page.register_code(directory, spec, name, None, server=False)

        # Repeated lease losses now request a decision; keep the human surface offline.
        patcher = patch('lessons.open_request', side_effect=open_decision)
        patcher.start()
        self.addCleanup(patcher.stop)

    def lease(self, key='PROJ-1', token='ticket', **extra):
        data = dict(key=key, token=token, parent='global', startedAt=time.time(),
                    deadline=time.time() + 9900, state='launching', orchestratorId=None)
        data.update(extra)
        LOCK.atomic_json(self.state / 'tickets' / (key + '.json'), data)
        LOCK.run_path(self.state, token).mkdir(parents=True, exist_ok=True)
        return data

    def command(self, action, *args):
        return self.cli('run_lock.py', action, '--dir', str(self.state), *args)

    def test_acquire_attach_owns_release_and_metadata(self):
        self.lock('global', startedAt=1000)
        response = self.command('lease-acquire', '--parent', 'global', '--key', 'PROJ-1')
        self.assertEqual(response.returncode, 0, response.stderr)
        token = response.stdout.strip()
        data = LOCK.lease_list(self.state)[0]
        self.assertEqual(data['state'], 'launching')
        self.assertEqual(data['deadline'] - data['startedAt'], 9900)
        with LOCK.locked(self.state):
            self.assertTrue(LOCK.owns_ticket(self.state, token, 'PROJ-1'))
            self.assertFalse(LOCK.owns(self.state, token))
            self.assertEqual(LOCK.run_meta(self.state, 'global')['deadline'], 10900)
            self.assertEqual(LOCK.run_meta(self.state, token)['key'], 'PROJ-1')
        self.assertEqual(self.command('lease-attach', '--key', 'PROJ-1', '--token', token,
                                      '--id', 'owner').returncode, 0)
        self.assertEqual(LOCK.lease_list(self.state)[0]['state'], 'live')
        self.assertEqual(self.command('lease-attach', '--key', 'PROJ-1', '--token', token,
                                      '--id', 'other').returncode, 1)
        self.assertEqual(self.command('lease-release', '--key', 'PROJ-1', '--token', 'old').returncode, 1)
        self.assertEqual(self.command('lease-release', '--key', 'PROJ-1', '--token', token).returncode, 0)
        self.assertEqual(LOCK.lease_list(self.state), [])
        self.assertTrue((self.state / 'runs' / token / 'ended').exists())
        with self.assertRaises(PermissionError):
            LOCK.run_meta(self.state, token)

    def test_two_process_acquire_race_and_capacity(self):
        self.lock('global')
        (self.state / 'lanes').write_text('1')
        argv = [sys.executable, str(SCRIPTS / 'run_lock.py'), 'lease-acquire',
                '--dir', str(self.state), '--parent', 'global', '--key']
        first = subprocess.Popen(argv + ['PROJ-1'], env=self.env, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True)
        second = subprocess.Popen(argv + ['PROJ-2'], env=self.env, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True)
        outputs = [first.communicate(timeout=15), second.communicate(timeout=15)]
        self.assertEqual(sorted([first.returncode, second.returncode]), [0, 1], outputs)
        self.assertEqual(len(LOCK.lease_list(self.state)), 1)
        self.assertEqual(LOCK.lanes(self.state), 1)
        (self.state / 'lanes').unlink()
        self.assertEqual(LOCK.lanes(self.state), 5)
        (self.state / 'lanes').write_text('0')
        denied = self.command('lease-acquire', '--parent', 'global', '--key', 'PROJ-3')
        self.assertEqual(denied.returncode, 1)
        self.assertEqual(json.loads(denied.stdout)['reason'], 'lanes-off')

    def test_two_process_same_key_acquire_allows_only_one_lease(self):
        self.lock('global')
        argv = [sys.executable, str(SCRIPTS / 'run_lock.py'), 'lease-acquire',
                '--dir', str(self.state), '--parent', 'global', '--key', 'PROJ-1']
        with LOCK.locked(self.state):
            processes = [subprocess.Popen(argv, env=self.env, stdout=subprocess.PIPE,
                                          stderr=subprocess.PIPE, text=True) for _ in range(2)]
        outputs = [process.communicate(timeout=15) for process in processes]
        self.assertEqual(sorted(process.returncode for process in processes), [0, 1], outputs)
        winner = next(output[0].strip() for process, output in zip(processes, outputs)
                      if process.returncode == 0)
        rejected = next(json.loads(output[0]) for process, output in zip(processes, outputs)
                        if process.returncode == 1)
        self.assertEqual(rejected, {'reason': 'lease-conflict', 'keys': ['PROJ-1']})
        leases = LOCK.lease_list(self.state)
        self.assertEqual([(lease['key'], lease['token']) for lease in leases], [('PROJ-1', winner)])
        self.assertEqual(sorted(path.name for path in (self.state / 'runs').iterdir()),
                         sorted(['global', winner]))

    def test_invalid_ticket_keys_fail_before_lease_side_effects(self):
        self.lock('global')
        before = (self.state / 'running.json').read_bytes()
        for key in ('proj-1', 'A-1', 'A20', 'PROJ-x', 'A20_1', '1A-1', '../PROJ-1',
                    'PROJ-1/../PROJ-2', '', None):
            with self.subTest(key=key):
                with self.assertRaises(ValueError):
                    LOCK.lease_path(self.state, key)
                with self.assertRaises(ValueError):
                    LOCK.read_lease(self.state, key)
                args = ['--parent', 'global'] + (['--key', key] if key is not None else [])
                self.assertEqual(self.command('lease-acquire', *args).returncode, 2)
                self.assertEqual((self.state / 'running.json').read_bytes(), before)
                self.assertEqual(LOCK.lease_list(self.state), [])
        self.assertEqual(self.recorded(), [])
        self.assertEqual([path.name for path in (self.state / 'runs').iterdir()], ['global'])

    def test_default_capacity_and_unreconciled_workers_block_acquire(self):
        self.lock('global')
        for index in range(5):
            self.lease(key='PROJ-' + str(index), token='ticket' + str(index))
        response = self.command('lease-acquire', '--parent', 'global', '--key', 'PROJ-5')
        self.assertEqual(response.returncode, 1)
        self.assertEqual(json.loads(response.stdout)['reason'], 'capacity')
        for path in (self.state / 'tickets').glob('*.json'):
            path.unlink()
        self.configure(agents=[self.row(token='ticket0', key='PROJ-4')])
        LOCK.mark(self.state, 'ticket0', 'lost')
        response = self.command('lease-acquire', '--parent', 'global', '--key', 'PROJ-4')
        self.assertEqual(response.returncode, 1)
        self.assertEqual(json.loads(response.stdout), {'reason': 'blocked', 'tokens': ['ticket0']})

    @patch('decision_inbox.alert_detached')
    def test_conflicts_include_all_states_and_bidirectional_relations(self, _notify):
        for left, right in (({'relatedKeys': ['PROJ-2']}, {}), ({}, {'relatedKeys': ['PROJ-1']}),
                            ({'branch': 'fix/shared'}, {'branch': 'fix/shared'}),
                            ({'carriedOn': 'PROJ-2'}, {}), ({}, {'carriedOn': 'PROJ-1'}),
                            ({'alsoKeys': ['PROJ-2']}, {}), ({}, {'alsoKeys': ['PROJ-1']})):
            for state in ('launching', 'live', 'unknown'):
                with self.subTest(left=left, right=right, state=state):
                    LOCK.atomic_json(self.state / 'followup-queue.json', {'PROJ-1': left, 'PROJ-2': right})
                    self.lease(state=state)
                    self.assertEqual(LOCK.lease_conflicts(self.state, 'PROJ-2'), ['PROJ-1'])
                    self.assertEqual(LOCK.lease_conflicts(self.state, 'PROJ-1'), ['PROJ-1'])
        self.lock('global')
        self.assertEqual(self.command('lease-acquire', '--parent', 'global', '--key', 'PROJ-2').returncode, 1)
        (self.state / 'lanes').write_text('0')
        self.assertEqual(LOCK.lease_conflicts(self.state, 'PROJ-2'), ['PROJ-1'])

    def test_launching_sweep_rescues_running_agent_and_expires_empty(self):
        self.lease(startedAt=1000)
        owner = self.row('owner', token='ticket', status='running')
        owner['labels']['role'] = 'ticket-orchestrator'
        self.configure(agents=[owner])
        self.assertEqual(LOCK.lease_sweep(self.state, now=1600), [])
        data = LOCK.lease_list(self.state)[0]
        self.assertEqual((data['state'], data['orchestratorId']), ('live', 'owner'))
        self.lease(startedAt=1000)
        self.configure(agents=[])
        self.assertEqual(LOCK.lease_sweep(self.state, now=1599), [])
        self.assertEqual(LOCK.lease_sweep(self.state, now=1600), ['PROJ-1'])
        self.assertTrue((self.state / 'runs/ticket/lost').exists())
        self.assertIn('lost-lease: PROJ-1 ticket', next(self.state.glob('followup-*.md')).read_text())
        self.assertEqual(json.loads((self.state / 'lessons.jsonl').read_text())['kind'], 'infra_fail')
        self.assertEqual(self.command('lease-attach', '--key', 'PROJ-1', '--token', 'ticket',
                                      '--id', 'late-owner').returncode, 1)
        self.assertEqual(self.command('owns-ticket', '--key', 'PROJ-1', '--token', 'ticket').returncode, 1)
        self.assertEqual(self.command('worker', '--key', 'PROJ-1', '--token', 'ticket').returncode, 3)

    def test_closed_archived_error_idle25_and_query_failure_sweep(self):
        for owner in ({'Archived': False, 'Status': 'closed'}, {'Archived': True, 'Status': 'idle'},
                      {'Archived': False, 'Status': 'error'}):
            with self.subTest(owner=owner):
                self.lease(state='live', orchestratorId='owner', startedAt=1000)
                self.configure(agents=[], inspect=owner)
                self.assertEqual(LOCK.lease_sweep(self.state, now=1000), ['PROJ-1'])
        self.lease(state='live', orchestratorId='owner', startedAt=1000)
        self.configure(inspect={'Archived': False, 'Status': 'idle'})
        self.assertEqual(LOCK.lease_sweep(self.state, now=1000), [])
        self.assertEqual(LOCK.lease_sweep(self.state, now=2499), [])
        self.assertEqual(LOCK.lease_sweep(self.state, now=2500), ['PROJ-1'])
        self.assertEqual(len(self.decisions), 1)
        self.assertEqual(self.decisions[0]['recommended'], 'fix-now')
        self.lease(startedAt=1000)
        self.configure(ls_fail=True)
        self.assertEqual(LOCK.lease_sweep(self.state, now=1600), [])
        self.assertEqual(LOCK.lease_list(self.state)[0]['queryFailures'], 1)

    def test_busy_global_guard_still_sweeps_leases(self):
        self.lock('global')
        self.lease(startedAt=time.time() - 601)
        self.configure(agents=[self.row(token='global', status='running')])
        response = self.command('guard', '--manual')
        self.assertEqual(response.returncode, 0, response.stderr)
        self.assertIn('skipping duplicate (running)', response.stdout)
        self.assertEqual(LOCK.lease_list(self.state), [])
        self.assertEqual(LOCK.read_lock(self.state)['token'], 'global')

    def test_refill_skips_once_gate_but_obeys_idle_scan(self):
        result = subprocess.CompletedProcess([], 0, json.dumps({'reasons': []}), '')
        with patch.object(LOCK.subprocess, 'run', return_value=result) as run:
            self.assertEqual(LOCK.main(['guard', '--refill', '--dir', str(self.state)]), 0)
        self.assertEqual(run.call_count, 1)
        self.assertTrue(run.call_args.args[0][1].endswith('followup_idle.py'))
        self.assertIsNone(LOCK.read_lock(self.state))
        result = subprocess.CompletedProcess([], 1, '', '')
        with patch.object(LOCK.subprocess, 'run', return_value=result):
            self.assertEqual(LOCK.main(['guard', '--refill', '--dir', str(self.state)]), 0)
        self.assertEqual(LOCK.read_lock(self.state)['mode'], 'tick')

    def test_scoped_worker_and_privileged_commands_reject_ticket_token(self):
        self.lease()
        for global_lock in (True, False):
            for mode in ('on', 'off'):
                with self.subTest(global_lock=global_lock, mode=mode):
                    (self.state / 'liveness').write_text(mode)
                    if global_lock:
                        self.lock('global')
                    else:
                        LOCK.clear(self.state)
                        (self.state / 'running').write_text(str(time.time()))
                    self.assertEqual(self.command('owns-ticket', '--token', 'ticket', '--key', 'PROJ-1').returncode, 0)
                    self.assertEqual(self.command('worker', '--token', 'ticket', '--key', 'PROJ-1').returncode, 0)
                    self.assertEqual(self.command('worker', '--token', 'ticket', '--key', 'PROJ-2').returncode, 3)
                    self.assertEqual(self.command('worker', '--token', 'stale', '--key', 'PROJ-1').returncode, 3)
                    for action, args in (
                        ('guard', ['--owns', 'ticket']), ('guard', ['--release', '--token', 'ticket']),
                        ('guard', ['--release', '--force', '--token', 'ticket']),
                        ('attach', ['--token', 'ticket', '--id', 'intruder']),
                        ('lease-acquire', ['--parent', 'ticket', '--key', 'PROJ-2']),
                        ('recover', ['--current-token', 'ticket', '--token', 'old']),
                        ('reconciled', ['--current-token', 'ticket', '--token', 'old'])):
                        with self.subTest(action=action):
                            self.assertEqual(self.command(action, *args).returncode, 1)
                    self.assertTrue((self.state / 'running').exists())

    def test_release_keeps_running_or_unknown_worker_lost_and_wrong_token_evidence(self):
        for config in ({'agents': [self.row(token='ticket', status='running')]}, {'ls_fail': True}):
            self.settings = {}
            self.configure(**config)
            self.lease(state='live', orchestratorId='owner')
            self.assertEqual(self.command('lease-release', '--key', 'PROJ-1', '--token', 'ticket').returncode, 0)
            self.assertTrue((self.state / 'runs/ticket/lost').exists())
            self.assertFalse((self.state / 'runs/ticket/ended').exists())
        self.lease(token='replacement')
        self.assertEqual(self.command('lease-release', '--key', 'PROJ-1', '--token', 'ticket').returncode, 1)
        self.assertEqual(LOCK.lease_list(self.state)[0]['token'], 'replacement')
        self.assertTrue((self.state / 'runs/ticket/lost').exists())

    def test_owner_release_ends_but_preexisting_lost_is_preserved(self):
        self.lease(state='live', orchestratorId='owner')
        self.configure(agents=[self.row('owner', token='ticket', status='running')])
        self.assertEqual(self.command('lease-release', '--key', 'PROJ-1', '--token', 'ticket').returncode, 0)
        self.assertTrue((self.state / 'runs/ticket/ended').exists())
        self.lease(token='lost-ticket', state='live', orchestratorId='owner')
        LOCK.mark(self.state, 'lost-ticket', 'lost')
        self.assertEqual(self.command('lease-release', '--key', 'PROJ-1', '--token', 'lost-ticket').returncode, 0)
        self.assertTrue((self.state / 'runs/lost-ticket/lost').exists())
        self.assertFalse((self.state / 'runs/lost-ticket/ended').exists())


if __name__ == '__main__':
    import unittest
    unittest.main()
