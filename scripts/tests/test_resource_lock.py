"""자원 잠금은 임시 상태와 PATH 첫 Paseo 스텁으로만 검증한다."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest
from unittest.mock import Mock, patch

from test_run_lock import LivenessFixture, SCRIPTS, STUB


class ResourceChecks(LivenessFixture):
    def acquire(self, name='runtime-local', token='owner', attempt='attempt-one', **extra):
        import resource_lock
        return resource_lock.acquire(self.state, name, token, 'PROJ-1', attempt,
                                     extra.pop('wait_until', time.time() + 1), **extra)

    def record(self, **extra):
        import resource_lock
        value = dict(name='runtime-local', token='old', key='PROJ-1', attempt='attempt-old',
                     at=time.time(), pid=99999999)
        value.update(extra)
        value.setdefault('pidStart', 'fixture-process-start-' + str(value['pid']))
        resource_lock.atomic_json(self.state / 'locks/runtime-local.json', value)
        return value

    def test_invalid_files_are_skipped_preserved_and_alerted_once_per_path(self):
        import resource_lock
        valid = self.record()
        root = self.state / 'locks'
        invalid = {'broken.json': b'{broken', 'shape.json': b'[]',
                   'record.json': b'{"name":"record","pid":0}'}
        for name, content in invalid.items():
            (root / name).write_bytes(content)
        notify = Mock()
        with patch.dict(sys.modules, decision_inbox=Mock(alert_detached=notify)):
            for _ in range(2):
                self.assertEqual(resource_lock.list_locks(self.state), [valid])
        self.assertEqual(notify.call_count, len(invalid))
        journal = ''.join(path.read_text() for path in self.state.glob('followup-*.md'))
        for name, content in invalid.items():
            self.assertEqual((root / name).read_bytes(), content)
            self.assertEqual(sum('liveness-alert: invalid ' in line and name in line
                                 for line in journal.splitlines()), 1)

    def test_invalid_owned_resource_blocks_acquire_and_release_without_overwrite(self):
        import resource_lock
        path = self.state / 'locks/runtime-local.json'
        path.parent.mkdir()
        original = b'{broken'
        path.write_bytes(original)
        notify = Mock()
        with patch.dict(sys.modules, decision_inbox=Mock(alert_detached=notify)):
            self.assertEqual(self.acquire(wait_until=time.time() + .02), 4)
            self.assertEqual(resource_lock.release(self.state, 'runtime-local', 'owner', 'attempt-one'), 4)
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(notify.call_count, 1)
        self.assertFalse(self.recorded())

    def test_new_record_saves_exact_stripped_ps_start_time(self):
        import resource_lock
        started = 'Fri Oct 30 09:12:34 2026'
        response = subprocess.CompletedProcess([], 0, '  ' + started + '\n', '')
        with patch.object(resource_lock.subprocess, 'run', return_value=response) as query:
            self.assertEqual(self.acquire(pid=os.getpid()), 0)
        saved = json.loads((self.state / 'locks/runtime-local.json').read_text())
        self.assertEqual(saved['pidStart'], started)
        self.assertEqual(query.call_args.args[0], ['ps', '-o', 'lstart=', '-p', str(os.getpid())])

    def test_missing_process_start_records_are_preserved_blocked_and_alerted_once(self):
        import resource_lock
        originals = {}
        for pid in (os.getpid(), 99999999):
            name = 'legacy-' + str(pid)
            path = self.state / 'locks' / (name + '.json')
            resource_lock.atomic_json(path, dict(name=name, token='old', key='PROJ-1',
                attempt='attempt-old', at=time.time(), pid=pid))
            originals[name] = path.read_bytes()
        notify = Mock()
        with patch.dict(sys.modules, decision_inbox=Mock(alert_detached=notify)):
            for _ in range(2):
                self.assertEqual(resource_lock.list_locks(self.state), [])
                for name in originals:
                    self.assertEqual(self.acquire(name=name, wait_until=time.time() + .02), 4)
                    self.assertEqual(resource_lock.release(self.state, name, 'old', 'attempt-old'), 4)
        for name, original in originals.items():
            self.assertEqual((self.state / 'locks' / (name + '.json')).read_bytes(), original)
        self.assertEqual(notify.call_count, 2)
        self.assertFalse(self.recorded())

    def test_pid_alive_requires_process_start_identity(self):
        import resource_lock
        with self.assertRaises(ValueError):
            resource_lock.pid_alive(os.getpid(), None)

    def test_reused_live_pid_is_reclaimed_only_after_attempt_is_stopped(self):
        import resource_lock
        self.record(pid=os.getpid(), pidStart='old process start')
        with patch.object(resource_lock, 'pid_start', return_value='new process start'):
            self.assertEqual(self.acquire(), 0)
        saved = json.loads((self.state / 'locks/runtime-local.json').read_text())
        self.assertEqual(saved['pidStart'], 'new process start')
        self.assertIn('attempt=attempt-old', self.recorded()[-1]['args'])

    def test_reused_pid_does_not_bypass_identity_check_on_idempotent_acquire(self):
        import resource_lock
        self.record(pid=os.getpid(), pidStart='old process start', token='owner', attempt='attempt-one')
        with patch.object(resource_lock, 'pid_start', return_value='new process start'):
            self.assertEqual(self.acquire(pid=os.getpid()), 0)
        saved = json.loads((self.state / 'locks/runtime-local.json').read_text())
        self.assertEqual(saved['pidStart'], 'new process start')
        self.assertTrue(self.recorded(), 'same PID with a different start must query the old attempt')

    def test_matching_process_identity_protects_prelaunch_without_agent_query(self):
        import resource_lock
        before = self.record(pid=os.getpid(), pidStart='same process start')
        with patch.object(resource_lock, 'pid_start', return_value='same process start'):
            self.assertEqual(self.acquire(wait_until=time.time() + .02), 4)
        self.assertEqual(json.loads((self.state / 'locks/runtime-local.json').read_text()), before)
        self.assertFalse(self.recorded())

    def test_reused_pid_still_preserves_running_attempt(self):
        import resource_lock
        before = self.record(pid=os.getpid(), pidStart='old process start')
        row = self.row(status='running')
        row['labels']['attempt'] = 'attempt-old'
        self.configure(agents=[row])
        # The deadline also bounds the stub paseo query; 0.2 s was shorter than a Python stub's
        # start-up under load, so the query timed out unrecorded (8 of 10 isolated runs on 2026-11-06).
        with patch.object(resource_lock, 'pid_start', return_value='new process start'):
            self.assertEqual(self.acquire(wait_until=time.time() + 2), 4)
        self.assertEqual(json.loads((self.state / 'locks/runtime-local.json').read_text()), before)
        self.assertIn('attempt=attempt-old', self.recorded()[-1]['args'])

    def test_process_start_query_failure_preserves_lock(self):
        import resource_lock
        before = self.record(pid=os.getpid(), pidStart='old process start')
        with patch.object(resource_lock, 'pid_start',
                          side_effect=OSError('ps unavailable')) as query:
            self.assertEqual(self.acquire(wait_until=time.time() + .02), 4)
        self.assertEqual(query.call_count, 1)
        self.assertEqual(json.loads((self.state / 'locks/runtime-local.json').read_text()), before)
        self.assertFalse(self.recorded())

    def test_record_lists_owner_bash_pid_and_idempotent_acquire(self):
        import resource_lock
        self.assertEqual(self.acquire(pid=os.getpid()), 0)
        rows = resource_lock.list_locks(self.state)
        self.assertEqual(rows[0]['pid'], os.getpid())
        self.assertEqual(rows[0]['attempt'], 'attempt-one')
        self.assertEqual(self.acquire(pid=os.getpid()), 0)
        self.assertEqual(resource_lock.list_locks(self.state), rows)

    def test_live_pid_protects_prelaunch_even_without_agent(self):
        self.record(pid=os.getpid())
        self.assertEqual(self.acquire(wait_until=time.time() + .05), 4)
        self.assertFalse(self.recorded())

    def test_dead_pid_and_absent_running_attempt_reclaims(self):
        self.record()
        self.assertEqual(self.acquire(), 0)
        args = self.recorded()[-1]['args']
        self.assertIn('attempt=attempt-old', args)
        self.assertNotIn('run=old', args)

    def test_live_20261102_dead_pid_closed_attempt_after_alert_is_reclaimed(self):
        # 실제 locks/deploy-web.json: PROJ-1469 작업자 pid 종료, attempt 에이전트 closed, 6시간 알림 이미 보냄.
        import resource_lock
        self.record(token='8520712a6bc24fafb27f162ccc75dcf5', key='PROJ-1469', attempt='attempt-54j9et60',
                    at=time.time() - 30000, alertedAt=time.time() - 8000, pidStart='Mon Nov  2 08:58:34 2026')
        row = self.row(status='closed')
        row['labels']['attempt'] = 'attempt-54j9et60'
        self.configure(agents=[row])
        with patch.object(resource_lock, 'notify_alert') as notify:
            self.assertEqual(self.acquire(token='next', attempt='attempt-next', pid=os.getpid()), 0)
        notify.assert_not_called()
        owner = json.loads((self.state / 'locks/runtime-local.json').read_text())
        self.assertEqual((owner['token'], owner['attempt'], owner['pid']), ('next', 'attempt-next', os.getpid()))
        self.assertNotIn('alertedAt', owner)

    def test_cli_default_pid_and_list_are_runtime_directory_scoped(self):
        response = self.cli('resource_lock.py', 'acquire', 'runtime-local', '--dir', str(self.state),
                            '--token', 'owner', '--key', 'PROJ-1', '--attempt', 'attempt-cli',
                            '--wait-until', str(time.time() + 2))
        self.assertEqual(response.returncode, 0, response.stderr)
        response = self.cli('resource_lock.py', 'list', '--dir', str(self.state))
        rows = json.loads(response.stdout)
        self.assertEqual(rows[0]['pid'], os.getpid())
        other = self.root / 'other'
        response = self.cli('resource_lock.py', 'list', '--dir', str(other))
        self.assertEqual(json.loads(response.stdout), [])

    def test_running_attempt_protects_dead_pid_without_ttl(self):
        self.record(at=1)
        row = self.row(status='running')
        row['labels']['attempt'] = 'attempt-old'
        self.configure(agents=[row])
        with patch('resource_lock.notify_alert'):
            self.assertEqual(self.acquire(wait_until=time.time() + .05), 4)

    def test_query_failures_and_malformed_lists_preserve(self):
        for setting in ({'ls_fail': True}, {'ls_invalid': '{}'}, {'ls_invalid': '[{}]'}):
            self.settings = {}
            self.configure(**setting)
            before = self.record()
            self.assertEqual(self.acquire(wait_until=time.time() + .05), 4)
            self.assertEqual(json.loads((self.state / 'locks/runtime-local.json').read_text()), before)

    def test_slow_paseo_query_cannot_overrun_shared_wait_deadline(self):
        self.record()
        (self.bin / 'paseo').write_text(STUB.replace('import json, os, sys', 'import json, os, sys, time')
            .replace("if args[:2] == ['agent', 'ls']:", "if args[:2] == ['agent', 'ls']:\n    time.sleep(5)"))
        started = time.monotonic()
        self.assertEqual(self.acquire(wait_until=time.time() + .1), 4)
        self.assertLess(time.monotonic() - started, 1)
        self.assertEqual(json.loads((self.state / 'locks/runtime-local.json').read_text())['token'], 'old')

    def test_flock_contention_obeys_wait_deadline_without_mutation(self):
        before = self.record()
        program = '''import fcntl, sys, time
from pathlib import Path
with Path(sys.argv[1]).open('a') as stream:
    fcntl.flock(stream, fcntl.LOCK_EX)
    print('locked', flush=True)
    time.sleep(1.2)
'''
        holder = subprocess.Popen([sys.executable, '-c', program, str(self.state / 'locks/.lock')],
                                  env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.assertEqual(holder.stdout.readline().strip(), 'locked')
            started = time.monotonic()
            response = self.cli('resource_lock.py', 'acquire', 'runtime-local', '--dir', str(self.state),
                                '--token', 'contender', '--key', 'PROJ-1', '--attempt', 'attempt-waiter',
                                '--wait-until', str(time.time() + .15))
            self.assertEqual(response.returncode, 4, response.stderr)
            self.assertLess(time.monotonic() - started, .8)
            self.assertIsNone(holder.poll(), 'waiter must time out while the other process still holds flock')
            self.assertEqual(json.loads((self.state / 'locks/runtime-local.json').read_text()), before)
            self.assertFalse(self.recorded())
        finally:
            holder.communicate(timeout=3)
        self.assertEqual(holder.returncode, 0)

    def test_release_requires_owner_attempt_and_confirmed_no_running(self):
        import resource_lock
        self.record(pid=os.getpid())
        self.assertEqual(resource_lock.release(self.state, 'runtime-local', 'wrong', 'attempt-old'), 3)
        self.assertEqual(resource_lock.release(self.state, 'runtime-local', 'old', 'wrong'), 3)
        self.configure(ls_fail=True)
        self.assertEqual(resource_lock.release(self.state, 'runtime-local', 'old', 'attempt-old'), 4)
        self.configure(ls_fail=False)
        row = self.row(status='running')
        row['labels']['attempt'] = 'attempt-old'
        self.configure(agents=[row])
        self.assertEqual(resource_lock.release(self.state, 'runtime-local', 'old', 'attempt-old'), 4)
        self.configure(agents=[])
        self.assertEqual(resource_lock.release(self.state, 'runtime-local', 'old', 'attempt-old'), 0)

    def test_six_hour_alert_once_while_preserving_live_owner(self):
        import resource_lock
        self.record(pid=os.getpid(), at=time.time() - 21601)
        with patch.object(resource_lock, 'notify_alert') as notify:
            for _ in range(2):
                self.assertEqual(self.acquire(wait_until=time.time() + .02), 4)
            self.assertEqual(notify.call_count, 1)
        self.assertEqual(resource_lock.list_locks(self.state)[0]['token'], 'old')

    def test_three_processes_share_runtime_local_without_overlap(self):
        program = '''import os, sys, time
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import resource_lock
d=Path(sys.argv[2]); token=sys.argv[3]
resource_lock.RETRY_SECONDS = .01
assert resource_lock.acquire(d, 'runtime-local', token, 'PROJ-1', token, time.time()+10, pid=os.getpid()) == 0
p=d/'entered'
with p.open('x') as s: s.write(token)
time.sleep(.08)
p.unlink()
assert resource_lock.release(d, 'runtime-local', token, token) == 0
'''
        children = [subprocess.Popen([sys.executable, '-c', program, str(SCRIPTS), str(self.state), str(i)],
                                    env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                    for i in range(3)]
        results = [(child, child.communicate(timeout=15)) for child in children]
        for child, (out, err) in results:
            self.assertEqual(child.returncode, 0, err.decode())
        self.assertEqual(list((self.state / 'locks').glob('*.json')), [])


if __name__ == '__main__':
    unittest.main()
