"""Retrospective regression evidence, entirely offline."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import teams_room
import drain


class Retrospective(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)
        self.now = datetime(2026, 10, 23, tzinfo=timezone.utc)

    def test_teams_cooldown_force_recovery_preserves_mark(self):
        # 가상의 감시 방과 호칭(config.env의 TEAMS_ROOM_CHAT_ID·TEAMS_ME_NAMES 자리).
        identity = patch.multiple(teams_room, CHAT_ID='19:room@example',
                                  TRIGGER=teams_room.trigger_pattern(['홍 ?선임']))
        identity.start()
        self.addCleanup(identity.stop)
        baseline = {'id':'1','time':self.now.isoformat(),'text':'일반 메시지','authorMri':'other'}
        request = {'id':'2','time':(self.now + timedelta(seconds=1)).isoformat(),
                   'text':'홍선임 확인 부탁','authorMri':'other'}
        teams_room.scan(self.directory, fetch=lambda: [baseline], now=self.now)
        initial = teams_room.scan(self.directory, fetch=lambda: [baseline,request], now=self.now)
        mark = json.loads((self.directory/'teams-room.json').read_text())[teams_room.CHAT_ID]['mark']
        self.assertEqual(len(initial['events']),1)
        fetch = Mock(side_effect=teams_room.Unavailable('TEAMS_WORKER_NOT_READY'))
        first = teams_room.scan(self.directory, fetch=fetch, now=self.now)
        later = teams_room.scan(self.directory, fetch=fetch, now=self.now + timedelta(seconds=1))
        self.assertEqual(fetch.call_count, 1)
        self.assertTrue(later['monitoring']['cooldown'])
        self.assertEqual(len(later['events']),1)
        self.assertEqual(json.loads((self.directory/'teams-room.json').read_text())[teams_room.CHAT_ID]['mark'],mark)
        self.assertEqual(first['monitoring']['retryAt'], (self.now + timedelta(seconds=1800)).isoformat())
        teams_room.scan(self.directory, fetch=fetch, now=self.now + timedelta(seconds=2), force_refresh=True)
        self.assertEqual(fetch.call_count, 2)
        success = teams_room.scan(self.directory, fetch=lambda: [], now=self.now + timedelta(seconds=3), force_refresh=True)
        self.assertEqual(success['monitoring']['failures'], 0)
        self.assertIsNone(success['monitoring']['error'])
        self.assertEqual(len(success['events']),1)

    def test_summary_unknown_usage_and_idempotency(self):
        import state_update
        state_update.atomic_json(self.directory/'running.json', {'token':'test', 'startedAt':1})
        (self.directory/'runs/test').mkdir(parents=True)
        drain.summary_init(self.directory, 'test', 1, 0, 1)
        drain.summary_record(self.directory, 'test', 'PROJ-1', 'build', 1, 3, 1, True)
        result = drain.summary_record(self.directory, 'test', 'PROJ-1', 'build', 1, 3, 1, True)
        self.assertIsNone(result['metrics']['tokens']['total'])
        self.assertIsNone(result['metrics']['tokens']['knownSubtotal'])
        self.assertEqual(result['metrics']['tokens']['unknownUnits'], 1)
        self.assertEqual(len(result['units']), 1)

    def result(self, **changes):
        return dict(status='done', summary='검증', key='PROJ-1', stage='built', waiting=None, question=None, suggest=None,
                    artifacts=['verify.json'], checks=[{'name':'runtime','result':'PASS'}], **changes)

    def test_required_gate_cli_blocks_failure_modes_retains_passed_evidence(self):
        import subprocess
        import state_update
        state_update.atomic_json(self.directory/'running.json', {'token':'test','startedAt':1})
        manifest = {'key':'PROJ-1','stage':'built','checks':[
            {'name':'runtime','artifact':'verify.json','resultField':'status','environment':{'environment':'dev'}},
            {'name':'api','artifact':'api.json','resultField':'status','environment':{'environment':'dev'}}]}
        state_update.atomic_json(self.directory/'required.json', manifest)
        for filename in ('verify.json','api.json'):
            state_update.atomic_json(self.directory/filename, {'status':'PASS','environment':'dev'})
        result = self.result()
        result['checks'].append({'name':'api','result':'PASS'})
        result['artifacts'].append('api.json')
        def gate(value):
            state_update.atomic_json(self.directory/'result.json', value)
            p = subprocess.run([sys.executable, str(Path(drain.__file__)), '--dir',str(self.directory),
                '--token','test','check-result','--key','PROJ-1','--result',str(self.directory/'result.json'),
                '--requirements',str(self.directory/'required.json')], capture_output=True,text=True)
            return p.returncode, json.loads(p.stdout)
        self.assertTrue(gate(result)[1]['stageComplete'])
        state_update.atomic_json(self.directory/'result.json.worker.json', {'key':'PROJ-1','exitCode':7})
        code, out = gate(result)
        self.assertEqual(code,4)
        self.assertFalse(out['stageComplete'])
        (self.directory/'result.json.worker.json').unlink()
        for change in ('missing','FAIL','SKIP','duplicate','wrong-key','wrong-stage','unlisted','mismatch','file-missing'):
            with self.subTest(change=change):
                value = json.loads(json.dumps(result))
                if change == 'missing': value['checks'].pop()
                elif change in ('FAIL','SKIP'): value['checks'][1]['result'] = change
                elif change == 'duplicate': value['checks'].append(value['checks'][1])
                elif change == 'wrong-key': value['key'] = 'PROJ-2'
                elif change == 'wrong-stage': value['stage'] = 'deployed'
                elif change == 'unlisted': value['artifacts'].pop()
                elif change == 'mismatch': state_update.atomic_json(self.directory/'api.json', {'status':'PASS','environment':'stg'})
                elif change == 'file-missing': (self.directory/'api.json').unlink()
                code, out = gate(value)
                self.assertEqual(code, 4)
                self.assertFalse(out['stageComplete'])
                self.assertFalse(out['retryAllowed'])
                self.assertIn('runtime', [r['name'] for r in out['gate']['passedChecks']])
                self.assertEqual(out['queueWaiting'], 'human-risk')
                state_update.atomic_json(self.directory/'api.json', {'status':'PASS','environment':'dev'})
        # VPN 대기 보고는 env 대기로 남아야 가드가 VPN 연결을 알린다(2026-10-29 PROJ-1441).
        waiting = json.loads(json.dumps(result))
        waiting.update(status='waiting', waiting='env-human')
        waiting['checks'][1]['result'] = 'FAIL'
        code, out = gate(waiting)
        self.assertEqual((code, out['queueWaiting']), (4, 'env-human'))
        waiting['key'] = 'PROJ-2'
        self.assertEqual(gate(waiting)[1]['queueWaiting'], 'human-risk')
        waiting['key'] = 'PROJ-1'
        state_update.atomic_json(self.directory/'result.json.worker.json', {'key':'PROJ-1','exitCode':7})
        self.assertEqual(gate(waiting)[1]['queueWaiting'], 'human-risk')
        (self.directory/'result.json.worker.json').unlink()

    def test_checkpoint_survives_nonzero_and_timeout_without_lock_release(self):
        import subprocess
        import worker_runtime
        for timeout in (None, 0.5):
            with self.subTest(timeout=timeout):
                attempt = worker_runtime.prepare(self.directory)
                checkpoint = attempt/'checkpoint.json'
                script = self.directory/'worker.py'
                script.write_text('import json, pathlib, sys, time\n'
                    + f'pathlib.Path({str(checkpoint)!r}).write_text({json.dumps(self.result())!r})\n'
                    + ('time.sleep(10)\n' if timeout else 'sys.exit(7)\n'))
                lock = self.directory/'running.json'
                lock.write_text('{"token":"live"}')
                code = worker_runtime.execute(attempt, [sys.executable,str(script)], key='PROJ-1',timeout=timeout)
                self.assertEqual(code, 124 if timeout else 7)
                receipt = json.loads((attempt/'worker.json').read_text())
                self.assertTrue(receipt['checkpointValid'])
                self.assertFalse(receipt['stageComplete'])
                self.assertEqual(json.loads(lock.read_text())['token'], 'live')
                self.assertFalse(worker_runtime.valid_result(checkpoint,key='PROJ-2'))
                self.assertFalse(worker_runtime.valid_result(checkpoint,key='PROJ-1',started_at=checkpoint.stat().st_mtime + 1))

    def test_measured_summary_mixed_coverage_and_actual_receipt(self):
        import state_update
        state_update.atomic_json(self.directory/'running.json', {'token':'test','startedAt':1})
        (self.directory/'runs/test').mkdir(parents=True)
        drain.summary_init(self.directory,'test',1,0,1)
        observed = self.directory/'observed.json'
        state_update.atomic_json(observed, dict(source='operator measured event log',tokens=100,cost=0.01,
            currency='USD',humanInterventions=1,waitingSeconds=10,reworkAttempts=1))
        receipt = self.directory/'worker.json'
        state_update.atomic_json(receipt,dict(key='PROJ-1',outcome='failed',durationSeconds=2,exitCode=7))
        one = drain.summary_record(self.directory,'test','PROJ-1','build',1,3,1,True,
                                   observations=observed,worker_metadata=receipt)
        self.assertEqual(one['metrics']['tokens']['total'],100)
        two = drain.summary_record(self.directory,'test','PROJ-1','build',4,6,1,True)
        self.assertIsNone(two['metrics']['tokens']['total'])
        self.assertEqual(two['metrics']['tokens']['knownSubtotal'],100)
        self.assertEqual(two['metrics']['tokens']['unknownUnits'],1)
        self.assertEqual(two['metrics']['workerOutcomes']['failed'],1)
        self.assertIsNone(two['metrics']['humanInterventions']['buttonDecisions'])
        self.assertEqual(two['wallElapsedSeconds'],5)
        self.assertEqual(two['durationSeconds'],4)

    def test_summary_counts_actual_buttons_once_and_never_polling(self):
        import state_update
        state_update.atomic_json(self.directory/'running.json', {'token':'test','startedAt':1})
        (self.directory/'runs/test').mkdir(parents=True)
        drain.summary_init(self.directory,'test',1,0,1)
        row = {'at':2,'by':'button','key':'PROJ-1','kind':'choice','option':'yes'}
        (self.directory/'decisions.jsonl').write_text(json.dumps(row)+'\n'+json.dumps(row)+'\n'
            +json.dumps({'at':2,'kind':'env-human','key':'PROJ-1'})+'\n')
        result=drain.summary_record(self.directory,'test','PROJ-1','build',1,3,1,True)
        self.assertEqual(result['metrics']['humanInterventions']['buttonDecisions'],1)
        self.assertIsNone(result['metrics']['waitingSeconds']['total'])

    def test_failed_preflight_still_records_stage_timing(self):
        import followup_idle
        import subprocess
        from unittest.mock import patch
        (self.directory/'followup-queue.json').write_text(json.dumps({'PROJ-1':{
            'waiting':'env-human','needs':'dev-db','stage':'intent'}}))
        def run(argv):
            if argv[0]=='scan':
                Path(argv[argv.index('--out')+1]).write_text('{"events":[],"errors":[]}')
                return subprocess.CompletedProcess(argv,0,'','')
            raise subprocess.TimeoutExpired(argv,300)
        ticks=iter(range(20))
        with patch.object(followup_idle,'run',side_effect=run):
            result,code=followup_idle.decide(self.directory,['scan'],['preflight'],clock=lambda:next(ticks))
        self.assertEqual(code,2)
        self.assertIn('dev-db',result['timings']['preflightSeconds'])
        self.assertEqual(result['monitoringStatus'],'degraded')

    def test_summary_cli_accepts_actual_keyless_worker_receipt(self):
        import subprocess
        import state_update
        state_update.atomic_json(self.directory/'running.json',{'token':'test','startedAt':1})
        (self.directory/'runs/test').mkdir(parents=True)
        drain.summary_init(self.directory,'test',0,0,1)
        result=self.directory/'morning.json'
        state_update.atomic_json(Path(str(result)+'.worker.json'),{'key':None,'outcome':'completed',
            'startedAt':1,'endedAt':3,'durationSeconds':2,'exitCode':0})
        p=subprocess.run([sys.executable,str(Path(drain.__file__)),'--dir',str(self.directory),
            '--token','test','summary-record','--unit','morning','--started-at','1','--ended-at','3',
            '--workers','1','--result',str(result)],capture_output=True,text=True)
        self.assertEqual(p.returncode,0,p.stderr)
        self.assertEqual(json.loads(p.stdout)['metrics']['workerOutcomes']['completed'],1)

    def test_legacy_waiting_or_failed_result_never_completes_stage(self):
        import worker_runtime
        for status,waiting in (('waiting','human-risk'),('failed',None)):
            with self.subTest(status=status):
                attempt=worker_runtime.prepare(self.directory)
                value=self.result()
                value.update(status=status,waiting=waiting)
                code=worker_runtime.execute(attempt,[sys.executable,'-c',
                    'import sys; sys.stdout.write('+repr(json.dumps(value))+')'],key='PROJ-1')
                self.assertEqual(code,0,'legacy process exit contract stays unchanged')
                receipt=json.loads((attempt/'worker.json').read_text())
                self.assertFalse(receipt['stageComplete'],
                                 'normal process exit cannot complete a waiting/failed stage')

    def test_monotonic_worker_receipt_is_retained_beside_wall_unit_interval(self):
        import state_update
        state_update.atomic_json(self.directory/'running.json',{'token':'test','startedAt':100})
        (self.directory/'runs/test').mkdir(parents=True)
        drain.summary_init(self.directory,'test',1,0,100)
        receipt=self.directory/'worker.json'
        state_update.atomic_json(receipt,{'key':'PROJ-1','outcome':'completed','startedAt':100,
            'endedAt':200,'durationSeconds':10,'exitCode':0})
        result=drain.summary_record(self.directory,'test','PROJ-1','build',100,200,1,True,
                                   worker_metadata=receipt)
        self.assertEqual(result['units'][0]['worker']['durationSeconds'],10)
        self.assertEqual(result['units'][0]['durationSeconds'],100)
