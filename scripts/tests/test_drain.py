"""외부 프로세스를 띄우지 않고 순차 drain의 경계와 장부를 재생한다."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
import drain
import run_lock
import state_update


class DrainTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='drain-offline-')
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)
        self.started = 1000000
        self.write('running.json', dict(token='test', startedAt=self.started))

    def write(self, name, value):
        state_update.atomic_json(self.directory / name, value)

    def cli(self, *args, token='test'):
        return subprocess.run([sys.executable, str(SCRIPTS / 'drain.py'), '--dir', str(self.directory),
                               '--token', token, *args], text=True, capture_output=True)

    def item(self, **fields):
        return dict(slug=None, stage='intent', service=None, branch=None,
                    commentIds=[], waiting=None, note='', **fields)

    def test_budget_uses_running_json_start_and_caps_default(self):
        self.write('running', 9)
        result = drain.next_budget(self.directory, 'test', '90m', self.started)
        self.assertEqual(result['deadline'], self.started + 9900)
        self.assertEqual(result['timeoutSeconds'], 5400)
        self.assertTrue(result['canStart'])
        self.assertTrue(result['drainEnabled'])

    def test_budget_boundary_and_shutdown_buffer(self):
        boundary = self.started + 9900 - 1800
        result = drain.next_budget(self.directory, 'test', 5400, boundary)
        self.assertTrue(result['canStart'])
        self.assertEqual(result['timeoutSeconds'], 1200)
        self.assertFalse(drain.next_budget(self.directory, 'test', 5400, boundary + 1)['canStart'])
        self.assertEqual(drain.next_budget(self.directory, 'test', 5400, self.started + 10000)['timeoutSeconds'], 0)

    def test_drain_zero_only_allows_first_unit_and_one_enables(self):
        (self.directory / 'drain').write_text('0\n')
        self.assertTrue(drain.next_budget(self.directory, 'test', 5400, self.started, units=0)['canStart'])
        self.assertFalse(drain.next_budget(self.directory, 'test', 5400, self.started, units=1)['canStart'])
        self.write('runs/test/summary.json', {'runtimeUnitCount':1})
        self.assertFalse(drain.next_budget(self.directory, 'test', 5400, self.started)['canStart'])
        (self.directory / 'drain').write_text('1\n')
        self.assertTrue(drain.next_budget(self.directory, 'test', 5400, self.started, units=3)['canStart'])

    def test_cli_budget_accepts_global_options_after_command(self):
        command = self.cli('next-budget', '--default-timeout', '60m', '--now', str(self.started), '--dir', str(self.directory))
        self.assertEqual(command.returncode, 0, command.stderr)
        self.assertEqual(json.loads(command.stdout)['timeout'], '3600s')
        self.assertEqual(self.cli('next-budget', token='other').returncode, 1)

    def test_order_approval_pending_focus_created_handled_number(self):
        queue = {f'PROJ-{i}': self.item() for i in range(1, 9)}
        queue['PROJ-8']['createdAt'] = '2020-01-01T00:00:00Z'
        queue['PROJ-7']['createdAt'] = '2021-01-01T00:00:00Z'
        queue['PROJ-6']['commentIds'] = ['old']
        ranked = drain.rank(queue, approvals={'PROJ-5': {'stg':'yes'}},
                            pending=['PROJ-4'], focus='PROJ-3', handled={'old': {'at':'2019-01-01T00:00:00Z'}})
        self.assertEqual(ranked, ['PROJ-5','PROJ-4','PROJ-3','PROJ-8','PROJ-7','PROJ-6','PROJ-1','PROJ-2'])

    def test_rank_skips_only_backoff_item_and_blocked_key(self):
        queue = {'PROJ-1': self.item(backoffUntil=200), 'PROJ-2':self.item(), 'PROJ-3':self.item()}
        self.assertEqual(drain.rank(queue, now=100, blocked=['PROJ-3']), ['PROJ-2'])
        self.assertEqual(drain.rank(queue, now=200, blocked=['PROJ-3']), ['PROJ-1','PROJ-2'])

    def test_env_wait_requires_explicit_runtime_eligibility(self):
        queue = {'PROJ-1': self.item()}
        queue['PROJ-1']['waiting'] = 'env-human'
        self.assertEqual(drain.rank(queue), [])
        self.assertEqual(drain.rank(queue, eligible=['PROJ-1']), ['PROJ-1'])
        self.assertEqual(drain.rank(queue, eligible=[], pending=['PROJ-1'], focus='PROJ-1',
                                    approvals={'PROJ-1':{'stg':'yes'}}), [])

    def test_golden_backup_replay_first_runtime_pick_1404(self):
        # 원본의 선정 필드만 복사한 fixture. 세 환경 대기는 preflight PASS 후 후보가 된다.
        fixture = json.loads((Path(__file__).parent / 'fixtures/drain-queue-20261023.json').read_text())
        self.write('followup-queue.json', fixture['queue'])
        replay = json.loads((self.directory / 'followup-queue.json').read_text())
        self.assertEqual(drain.rank(replay, eligible=fixture['eligibleAfterPreflight'])[0], 'PROJ-1404')
        self.assertEqual(len(fixture['sha256']), 64)

    def test_original_backup_copy_replay_and_sha_when_available(self):
        source = Path.home() / '.jira-followup/runs/followup-queue.bak-20261023-0815.json'
        if not source.exists():
            self.skipTest('원본 백업이 없는 PC에서는 위의 고정 fixture를 재생한다')
        fixture = json.loads((Path(__file__).parent / 'fixtures/drain-queue-20261023.json').read_text())
        before = hashlib.sha256(source.read_bytes()).hexdigest()
        self.assertEqual(before, fixture['sha256'])
        shutil.copy2(source, self.directory / 'followup-queue.json')
        command = self.cli('rank', '--eligible', *fixture['eligibleAfterPreflight'])
        self.assertEqual(command.returncode, 0, command.stderr)
        self.assertEqual(json.loads(command.stdout), ['PROJ-1404', 'PROJ-1379', 'PROJ-1389'])
        self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), before)

    def test_hash_ignores_all_bookkeeping_but_tracks_real_changes(self):
        item = self.item()
        changed = dict(item, **dict.fromkeys(drain.BOOKKEEPING, 42))
        self.assertEqual(drain.item_digest(item), drain.item_digest(changed))
        changed['note'] = 'changed'
        self.assertNotEqual(drain.item_digest(item), drain.item_digest(changed))

    def test_summary_actual_workers_durations_and_idempotent_record(self):
        first = self.cli('summary-init', '--runnable','4','--lane-eligible','0','--now',str(self.started))
        self.assertEqual(first.returncode, 0, first.stderr)
        record = ('summary-record','--key','PROJ-1','--unit','runtime','--started-at',str(self.started + 1),
                  '--ended-at',str(self.started + 31),'--workers','2','--runtime')
        for _ in range(2):
            done = self.cli(*record)
            self.assertEqual(done.returncode, 0, done.stderr)
        result = json.loads(done.stdout)
        self.assertEqual(result['workers'], 2)
        self.assertEqual(result['runtimeUnitCount'], 1)
        self.assertEqual(result['durationSeconds'], 30)
        self.assertEqual(result['startedAt'], self.started)
        again = self.cli('summary-init','--runnable','0','--lane-eligible','1')
        self.assertEqual(json.loads(again.stdout)['workers'], 2)
        self.assertEqual(self.cli(*record, token='other').returncode, 1)

    def test_summary_failure_does_not_mutate_existing_summary(self):
        self.assertEqual(self.cli('summary-init','--runnable','4','--lane-eligible','0').returncode, 0)
        before = (self.directory / 'runs/test/summary.json').read_bytes()
        invalid = self.cli('summary-record','--key','PROJ-1','--unit','runtime','--started-at','10',
                           '--ended-at','9','--workers','1','--runtime')
        self.assertEqual(invalid.returncode, 2)
        self.assertEqual((self.directory / 'runs/test/summary.json').read_bytes(), before)

    def test_defer_once_per_run_and_processed_reset(self):
        self.write('followup-queue.json', {'PROJ-1': self.item()})
        for offset in (1,2):
            command = self.cli('defer','--key','PROJ-1','--reason','budget','--now',str(self.started + offset))
            self.assertEqual(command.returncode, 0, command.stderr)
        item = json.loads((self.directory / 'followup-queue.json').read_text())['PROJ-1']
        self.assertEqual(item['deferCount'], 1)
        self.assertIn('createdAt', item)
        command = self.cli('processed','--key','PROJ-1','--now',str(self.started + 3))
        self.assertEqual(command.returncode, 0, command.stderr)
        item = json.loads((self.directory / 'followup-queue.json').read_text())['PROJ-1']
        self.assertEqual(item['deferCount'], 0)
        self.assertNotIn('deferredAt', item)

    def test_changed_hash_restarts_defer_count_even_with_same_run_timestamp(self):
        item = self.item(deferCount=3, deferredAt=drain.iso(self.started + 1))
        original = drain.item_digest(item)
        item['note'] = '실제 단계 변경'
        self.write('followup-queue.json', {'PROJ-1': item})
        self.write('guard-state.json', {'runnable': {'PROJ-1': {'hash': original}}})
        command = self.cli('defer','--key','PROJ-1','--reason','budget','--now',str(self.started+2))
        self.assertEqual(command.returncode, 0, command.stderr)
        self.assertEqual(json.loads(command.stdout)['PROJ-1']['deferCount'], 1)

    def test_rate_limit_item_backoff_progression_and_success_clear(self):
        self.write('followup-queue.json', {'PROJ-1':self.item(), 'PROJ-2':self.item()})
        for index, expected in enumerate((1800,3600,7200,7200)):
            now = self.started + index * 10000
            self.write('running.json', dict(token='test', startedAt=now))
            command = self.cli('rate-limit','--key','PROJ-1','--now',str(now + 1))
            self.assertEqual(command.returncode, 0, command.stderr)
            queue = json.loads((self.directory / 'followup-queue.json').read_text())
            item = queue['PROJ-1']
            self.assertEqual(drain.epoch(item['backoffUntil']) - now - 1, expected)
            self.assertEqual(item['deferReason'], 'rate-limit')
            self.assertEqual(drain.rank(queue, now=now+1), ['PROJ-2'])
        self.assertEqual(self.cli('processed','--key','PROJ-1').returncode, 0)
        item = json.loads((self.directory / 'followup-queue.json').read_text())['PROJ-1']
        self.assertNotIn('backoffUntil', item)
        self.assertNotIn('backoffLevel', item)

    def test_worker_result_or_stderr_rate_limit_is_infra_without_retry(self):
        self.write('followup-queue.json', {'PROJ-1':self.item()})
        result = self.directory/'result.json'
        stderr = self.directory/'stderr.txt'
        result.write_text('{"status":"done"}')
        stderr.write_text('secret fixture: HTTP 429 overloaded')
        command = self.cli('check-result','--key','PROJ-1','--result',str(result),
                           '--stderr',str(stderr),'--now',str(self.started + 1))
        self.assertEqual(command.returncode, 0, command.stderr)
        self.assertEqual(json.loads(command.stdout), dict(rateLimited=True, retryAllowed=False,
                         failureType='infra', deferReason='rate-limit'))
        self.assertNotIn('secret', command.stdout + command.stderr)
        item = json.loads((self.directory/'followup-queue.json').read_text())['PROJ-1']
        self.assertEqual(item['deferReason'], 'rate-limit')
        self.assertTrue(drain.backoff_active(item, self.started + 1))
        same = self.cli('check-result','--key','PROJ-1','--result',str(result),
                        '--stderr',str(stderr),'--now',str(self.started+2))
        self.assertEqual(same.returncode, 0, same.stderr)
        updated = json.loads((self.directory/'followup-queue.json').read_text())['PROJ-1']
        self.assertEqual(updated['backoffUntil'], item['backoffUntil'])
        self.assertEqual(updated['backoffLevel'], item['backoffLevel'])
        for text in ('rate limit', 'RATE-LIMIT', 'overloaded', 'HTTP 429',
                     'Selected model is at capacity'):
            result.write_text(text)
            self.assertTrue(drain.rate_limited(result))
        result.write_text('PROJ-1429 passed')
        self.assertFalse(drain.rate_limited(result))
        stderr.write_text('')
        command = self.cli('check-result','--key','PROJ-1','--result',str(result),'--stderr',str(stderr))
        self.assertEqual(json.loads(command.stdout), dict(rateLimited=False, retryAllowed=True))

    def test_changed_content_does_not_expand_same_run_duplicate_rate_limit(self):
        item = self.item()
        self.write('followup-queue.json', {'PROJ-1': item})
        self.write('guard-state.json', {'runnable': {'PROJ-1': {'hash': drain.item_digest(item)}}})
        state_update.patch(self.directory, 'followup-queue.json', ['PROJ-1'], {'note': '작업자 갱신'})
        for offset in (1, 2):
            command = self.cli('rate-limit', '--key', 'PROJ-1', '--now', str(self.started + offset))
            self.assertEqual(command.returncode, 0, command.stderr)
            row = json.loads(command.stdout)['PROJ-1']
            self.assertEqual(row['backoffLevel'], 0)
            self.assertEqual(drain.epoch(row['backoffUntil']), self.started + 1 + 1800)

    def test_validation_rejects_corrupt_or_incomplete_queue(self):
        self.write('followup-queue.json', {'PROJ-1':self.item()})
        self.assertEqual(self.cli('validate-queue').returncode, 0)
        self.write('followup-queue.json', {'PROJ-1': {'waiting': None}})
        self.assertEqual(self.cli('validate-queue').returncode, 2)
        (self.directory/'followup-queue.json').write_text('{broken')
        self.assertEqual(self.cli('validate-queue').returncode, 2)

    def test_pending_focus_cli_order_and_invalid_json(self):
        self.write('followup-queue.json', {'PROJ-1':self.item(), 'PROJ-2':self.item()})
        (self.directory/'pending-focus.jsonl').write_text('{"key":"PROJ-2"}\n')
        command = self.cli('rank')
        self.assertEqual(command.returncode, 0, command.stderr)
        self.assertEqual(json.loads(command.stdout), ['PROJ-2','PROJ-1'])
        (self.directory/'pending-focus.jsonl').write_text('broken\n')
        self.assertEqual(self.cli('rank').returncode, 2)


if __name__ == '__main__':
    unittest.main()
