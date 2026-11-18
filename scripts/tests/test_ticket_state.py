"""티켓 토큰의 쓰기 범위와 회수 후 원자적 거부를 임시 상태에서 검증한다."""
import fcntl
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
import state_update


class TicketStateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='ticket-state-')
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)
        self.write('running.json', dict(token='parent', startedAt=time.time()))
        self.write('tickets/PROJ-1.json', dict(token='ticket', key='PROJ-1', state='launching',
                   startedAt=time.time(), deadline=time.time() + 9900, parent='parent'))

    def write(self, path, value):
        state_update.atomic_json(self.directory / path, value)

    def argv(self, *args, token='ticket', key='PROJ-1'):
        return [sys.executable, str(SCRIPTS / 'state_update.py'), '--dir', str(self.directory),
                '--token', token, '--key', key, *args]

    def cli(self, *args, **kwargs):
        return subprocess.run(self.argv(*args, **kwargs), text=True, capture_output=True, timeout=10)

    def test_ticket_patch_preserves_sibling_and_rejects_other_paths(self):
        self.write('followup-queue.json', {'PROJ-1': {'note': 'old'}, 'PROJ-2': {'note': 'keep'}})
        result = self.cli('patch', 'followup-queue.json', '--path', '["PROJ-1"]', '--json', '{"note":"new"}')
        self.assertEqual(result.returncode, 0, result.stderr)
        before = (self.directory / 'followup-queue.json').read_bytes()
        for filename, path in [('followup-queue.json', '["PROJ-2"]'),
                               ('followup-queue.json', '[]'), ('approvals.json', '["PROJ-2"]'),
                               ('state.json', '["followup"]'), ('guard-state.json', '["PROJ-1"]')]:
            with self.subTest(filename=filename, path=path):
                self.assertEqual(self.cli('patch', filename, '--path', path, '--json', '{}').returncode, 3)
        self.assertEqual(before, (self.directory / 'followup-queue.json').read_bytes())
        self.assertEqual(json.loads(before)['PROJ-2'], {'note': 'keep'})

    def test_inbox_idempotence_ack_scope_and_consume(self):
        event = json.dumps(dict(key='PROJ-1', commentId='123', text='reply'))
        for _ in range(2):
            result = self.cli('append-inbox', '--key', 'PROJ-1', '--json', event)
            self.assertEqual(result.returncode, 0, result.stderr)
        inbox = self.directory / 'tickets/PROJ-1.inbox.jsonl'
        self.assertEqual(len(inbox.read_text().splitlines()), 1)
        self.assertFalse((self.directory / 'state.json').exists())
        ack = self.cli('patch', 'state.json', '--path', '["followup","handled","123"]',
                       '--json', '{"key":"PROJ-1","action":"done"}')
        self.assertEqual(ack.returncode, 0, ack.stderr)
        self.assertEqual(self.cli('patch', 'state.json', '--path', '["followup","handled","other"]',
                                  '--json', '{"key":"PROJ-1"}').returncode, 3)
        self.assertEqual(self.cli('append-inbox', '--key', 'PROJ-2', '--json', event).returncode, 3)
        self.assertEqual(self.cli('consume-inbox', '--key', 'PROJ-1', '--ids', '["123"]').returncode, 0)
        self.assertEqual(inbox.read_text(), '')

    def test_pending_ticket_scope_and_global_compatibility(self):
        self.assertEqual(self.cli('append-pending', '--json', '{"key":"PROJ-2"}').returncode, 3)
        good = self.cli('append-pending', '--json', '{"key":"PROJ-1"}')
        self.assertEqual(good.returncode, 0, good.stderr)
        self.assertEqual(self.cli('consume-pending', '--lines', json.dumps([json.dumps({'key': 'PROJ-2'})])).returncode, 3)
        self.assertEqual(self.cli('patch', 'approvals.json', '--path', '["PROJ-2"]',
                                  '--json', '{"stg":"yes"}', token='parent').returncode, 0)

    def test_guard_inbox_checks_lease_without_ack(self):
        event = dict(key='PROJ-1', commentId='c1')
        self.assertTrue(state_update.guard_append_inbox(self.directory, 'PROJ-1', event))
        self.assertTrue(state_update.guard_append_inbox(self.directory, 'PROJ-1', event))
        self.assertEqual(len((self.directory / 'tickets/PROJ-1.inbox.jsonl').read_text().splitlines()), 1)
        (self.directory / 'tickets/PROJ-1.json').unlink()
        self.assertFalse(state_update.guard_append_inbox(self.directory, 'PROJ-1', dict(event, commentId='c2')))
        self.assertFalse((self.directory / 'state.json').exists())

    def test_malformed_inbox_line_is_skipped_kept_and_alerted_once(self):
        inbox = self.directory / 'tickets/PROJ-1.inbox.jsonl'
        inbox.write_text('{broken\n' + json.dumps({'key': 'PROJ-1', 'commentId': 'c0'}) + '\n')
        with patch('run_lock.invalid_alert') as alert:
            self.assertTrue(state_update.guard_append_inbox(self.directory, 'PROJ-1', dict(key='PROJ-1', commentId='c1')))
            self.assertEqual([row['commentId'] for row in state_update.read_inbox(self.directory, 'PROJ-1')], ['c0', 'c1'])
            self.assertEqual(state_update.consume_inbox(self.directory, 'PROJ-1', ['c0']), 1)
        self.assertTrue(alert.called)
        self.assertEqual(inbox.read_text().splitlines()[0], '{broken')
        self.assertEqual([row['commentId'] for row in state_update.read_inbox(self.directory, 'PROJ-1')], ['c1'])

    def test_writer_waits_for_run_lock_then_rechecks_revoked_token(self):
        with (self.directory / '.run.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            child = subprocess.Popen(self.argv('patch', 'approvals.json', '--path', '["PROJ-1"]',
                                              '--json', '{"stg":"yes"}'),
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                time.sleep(0.15)
                self.assertIsNone(child.poll(), 'writer must wait on .run.lock before state write')
                (self.directory / 'tickets/PROJ-1.json').unlink()
                fcntl.flock(lock, fcntl.LOCK_UN)
                child.communicate(timeout=10)
                self.assertEqual(child.returncode, 3)
                self.assertFalse((self.directory / 'approvals.json').exists())
            finally:
                if child.poll() is None:
                    child.kill()
                    child.communicate()


if __name__ == '__main__':
    unittest.main()
