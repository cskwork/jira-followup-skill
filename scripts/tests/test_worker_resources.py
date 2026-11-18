"""자원 대기·실행·해제 경계는 실제 셸과 오프라인 Paseo 스텁으로 확인한다."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest

from test_run_lock import LivenessFixture, LOCK, SCRIPTS, STUB


class WorkerResourceChecks(LivenessFixture):
    def setUp(self):
        super().setUp()
        self.brief = self.root / 'brief.md'
        self.brief.write_text('목표: 자원 검증\n')
        self.lock('new')
        (self.root / 'api').mkdir(exist_ok=True)
        extra = '''elif args[0] == 'run':
    root=Path(os.environ['JIRA_WAKE_DIR'])
    locks=[json.loads(p.read_text()) for p in sorted((root/'locks').glob('*.json'))]
    (root/'resources-during-run.json').write_text(json.dumps(locks))
    if config.get('after_run_ls_fail'):
        config['ls_fail']=True
    if config.get('keep_running'):
        labels=dict(args[i+1].split('=',1) for i,v in enumerate(args) if v=='--label')
        config['agents']=[dict(id='worker',status='running',labels=labels)]
    if config.get('missing_stdout'):
        (Path(os.environ['JIRA_WORKER_ATTEMPT'])/'stdout.json').unlink()
    Path(os.environ['PASEO_FIXTURE']).write_text(json.dumps(config))
'''
        (self.bin / 'paseo').write_text(STUB.replace("elif args[0] == 'run':\n", extra))

    def worker(self, *extra):
        timeout = [extra[0]] if extra and not extra[0].startswith('--') else []
        extra = extra[len(timeout):]
        return self.cli('jira-worker', 'sol', 'build', str(self.brief),
                        *timeout, '--token', 'new', '--key', 'PROJ-1', '--resources', 'runtime-local', *extra)

    def locks(self):
        return list((self.state / 'locks').glob('*.json'))

    def receipt(self):
        return json.loads(next((self.state / 'runs/new').glob('attempt-*/worker.json')).read_text())

    def test_sorted_acquire_attempt_label_pid_and_completed_release(self):
        response = self.worker('--resources', 'runtime-local,deploy-api,runtime-local')
        self.assertEqual(response.returncode, 0, response.stderr)
        rows = json.loads((self.state / 'resources-during-run.json').read_text())
        self.assertEqual([row['name'] for row in rows], ['deploy-api', 'runtime-local'])
        self.assertLessEqual(rows[0]['at'], rows[1]['at'])
        self.assertEqual(rows[0]['pid'], rows[1]['pid'])
        self.assertGreater(rows[0]['pid'], 0)
        args = next(row['args'] for row in self.recorded() if row['args'][0] == 'run')
        self.assertIn('attempt=' + rows[0]['attempt'], args)
        self.assertEqual(self.locks(), [])

    def test_unknown_resource_name_is_refused_before_any_lock(self):
        response = self.worker('--resources', 'deploy-missing-web')
        self.assertEqual(response.returncode, 2)
        self.assertIn('모르는 자원 deploy-missing-web', response.stderr)
        self.assertEqual(self.locks(), [])
        self.assertFalse(list((self.state / 'runs/new').glob('attempt-*')))

    def test_failed_receipt_releases_confirmed_stopped_attempt(self):
        self.configure(run_rc=7)
        response = self.worker()
        self.assertEqual(response.returncode, 7, response.stderr)
        self.assertEqual(self.receipt()['outcome'], 'failed')
        self.assertEqual(self.locks(), [])

    def test_timeout_running_and_query_failure_preserve_locks(self):
        for setting in ({'run_rc': 124}, {'keep_running': True}, {'after_run_ls_fail': True}):
            with self.subTest(setting=setting):
                for lock in self.locks():
                    lock.unlink()
                self.settings = {}
                self.configure(**setting)
                response = self.worker()
                self.assertEqual(response.returncode, setting.get('run_rc', 0), response.stderr)
                self.assertEqual(len(self.locks()), 1)

    def test_missing_execution_result_preserves_lock(self):
        self.configure(missing_stdout=True)
        response = self.worker()
        self.assertEqual(response.returncode, 127, response.stderr)
        self.assertEqual(self.receipt()['outcome'], 'launch-failed')
        self.assertEqual(len(self.locks()), 1)

    def test_requested_timeout_under_fifteen_minutes_releases_without_launch(self):
        response = self.worker('899s')
        self.assertEqual(response.returncode, 4, response.stderr)
        self.assertEqual(self.locks(), [])
        self.assertFalse(any(row['args'][0] == 'run' for row in self.recorded()))

    def test_timeout_is_capped_by_remaining_run_deadline(self):
        self.lock('new', startedAt=time.time() - 80*60)
        response = self.worker('120m')
        self.assertEqual(response.returncode, 0, response.stderr)
        args = next(row['args'] for row in self.recorded() if row['args'][0] == 'run')
        seconds = int(args[args.index('--wait-timeout') + 1][:-1])
        self.assertGreater(seconds, 4400)
        self.assertLessEqual(seconds, 4500)

    def test_shared_wait_deadline_and_partial_acquire_release(self):
        import resource_lock
        # 첫 자원은 즉시 획득하고, 두 번째 자원은 같은 실행 마감까지만 기다린다.
        resource_lock.atomic_json(self.state / 'locks/runtime-local.json',
            dict(name='runtime-local',token='other',key='PROJ-2',attempt='other-attempt',at=time.time(),pid=os.getpid(),
                 pidStart='fixture-process-start-' + str(os.getpid())))
        self.lock('new', startedAt=time.time() - (165*60-1500-.7))
        started = time.monotonic()
        response = self.worker('--resources', 'runtime-local,deploy-api')
        self.assertEqual(response.returncode, 4, response.stderr)
        self.assertLess(time.monotonic() - started, 3)
        self.assertEqual([path.stem for path in self.locks()], ['runtime-local'])
        self.assertFalse(any(row['args'][0] == 'run' for row in self.recorded()))

    def test_stale_token_after_acquire_releases_and_refuses_launch(self):
        import resource_lock
        resource_lock.atomic_json(self.state / 'locks/runtime-local.json',
            dict(name='runtime-local', token='other', key='PROJ-2', attempt='other-attempt',
                 at=time.time(), pid=os.getpid(), pidStart='fixture-process-start-' + str(os.getpid())))
        child = subprocess.Popen(['/bin/bash', str(SCRIPTS / 'jira-worker'), 'sol', 'build', str(self.brief),
            '--token','new','--key','PROJ-1','--resources','runtime-local'],
            env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.addCleanup(lambda: child.poll() is None and child.kill())
        # attempt 생성 후 자원 대기에 진입한 것을 기다린 뒤 소유권을 교체한다.
        limit = time.monotonic() + 5
        while not list((self.state / 'runs/new').glob('attempt-*')) and time.monotonic() < limit:
            time.sleep(.01)
        time.sleep(.15)
        self.lock('replacement')
        (self.state / 'locks/runtime-local.json').unlink()
        out, err = child.communicate(timeout=20)
        self.assertEqual(child.returncode, 3, err.decode())
        self.assertEqual(self.locks(), [])
        self.assertFalse(any(row['args'][0] == 'run' for row in self.recorded()))

    def test_ticket_owner_prompt_scopes_mutations(self):
        token='ticket-owner'
        LOCK.atomic_json(self.state / 'tickets/PROJ-1.json', dict(token=token,key='PROJ-1',state='launching',
            startedAt=time.time(),deadline=time.time()+7200,parent='new'))
        response = self.worker('--token', token)
        self.assertEqual(response.returncode, 0, response.stderr)
        prompt = next(row['args'][-1] for row in self.recorded() if row['args'][0] == 'run')
        self.assertIn('owns-ticket --dir', prompt)
        self.assertIn('--token <run_token> --key <ticket_key> lesson', prompt)
        self.assertIn('Follow §0-1', prompt)  # patch 형식은 RUNBOOK §0-1


if __name__ == '__main__':
    unittest.main()
