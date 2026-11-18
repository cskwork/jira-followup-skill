"""상위 실행 종료·교체 뒤에도 티켓의 예산과 장부가 유지되는지 검증한다."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
import drain
import state_update


class TicketDrainTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='ticket-drain-')
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)
        self.started = 1000000
        self.deadline = self.started + 5000
        self.write('tickets/PROJ-1.json', dict(token='ticket', key='PROJ-1', state='live',
                   parent='old-parent', startedAt=self.started, deadline=self.deadline))
        self.write('running.json', dict(token='new-parent', startedAt=self.started + 4000))
        self.write('followup-queue.json', {'PROJ-1': self.item(deferCount=3), 'PROJ-2': self.item()})

    def write(self, name, value):
        state_update.atomic_json(self.directory / name, value)

    def item(self, **values):
        return dict(slug=None, stage='intent', service=None, branch=None,
                    commentIds=[], waiting=None, note='', **values)

    def cli(self, *args, token='ticket'):
        return subprocess.run([sys.executable, str(SCRIPTS / 'drain.py'), '--dir', str(self.directory),
                               '--token', token, *args], text=True, capture_output=True, timeout=10)

    def test_budget_and_summary_use_ticket_deadline_after_global_release(self):
        (self.directory / 'running.json').unlink()
        budget = drain.next_budget(self.directory, 'ticket', '90m', now=self.started)
        self.assertEqual(budget['deadline'], self.deadline)
        self.assertEqual(budget['timeoutSeconds'], 4400)
        self.assertTrue(budget['canStart'])
        result = self.cli('summary-init', '--runnable', '1', '--lane-eligible', '1', '--now', str(self.started))
        self.assertEqual(result.returncode, 0, result.stderr)
        summary = json.loads(result.stdout)
        self.assertEqual(summary['startedAt'], self.started)
        self.assertEqual(summary['deadline'], self.deadline)
        record = self.cli('summary-record', '--key', 'PROJ-1', '--unit', 'build',
                          '--started-at', str(self.started), '--ended-at', str(self.started + 30),
                          '--workers', '1', '--runtime')
        self.assertEqual(record.returncode, 0, record.stderr)
        self.assertEqual(json.loads(record.stdout)['workers'], 1)

    def test_cross_ticket_and_revoked_validate_or_update_are_rejected(self):
        for command in [('processed', '--key', 'PROJ-2'),
                        ('check-result', '--key', 'PROJ-2', '--result', str(self.directory / 'missing'))]:
            with self.subTest(command=command):
                self.assertEqual(self.cli(*command).returncode, 1)
        (self.directory / 'tickets/PROJ-1.json').unlink()
        self.assertEqual(self.cli('validate-queue').returncode, 1)
        self.assertEqual(self.cli('processed', '--key', 'PROJ-1').returncode, 1)

    def test_capacity_and_resource_defer_preserve_counter_across_runs(self):
        for reason in ('capacity', 'resource', 'capacity'):
            result = self.cli('defer', '--key', 'PROJ-1', '--reason', reason,
                              '--now', str(self.started + 100))
            self.assertEqual(result.returncode, 0, result.stderr)
            row = json.loads(result.stdout)['PROJ-1']
            self.assertEqual(row['deferCount'], 3)
            self.assertEqual(row['deferReason'], reason)

    def test_rank_excludes_all_lease_conflicts_even_when_lanes_zero(self):
        (self.directory / 'lanes').write_text('0\n')
        self.write('followup-queue.json', {'PROJ-1': self.item(),
                   'PROJ-2': self.item(relatedKeys=['PROJ-1']), 'PROJ-3': self.item()})
        result = self.cli('rank', token='new-parent')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), ['PROJ-3'])


if __name__ == '__main__':
    unittest.main()
