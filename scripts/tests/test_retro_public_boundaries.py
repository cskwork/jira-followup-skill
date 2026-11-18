"""Public CLI behavior, also runnable unchanged against the pre-change checkout.

RETRO_BOUNDARY_SCRIPTS selects an archived baseline for assertion-level RED.
No new helper imports or newly added Python keywords are needed for that run.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPTS = Path(os.environ.get('RETRO_BOUNDARY_SCRIPTS', str(Path(__file__).resolve().parents[1])))


class PublicBoundaryEvidence(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='retro-public-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.state = self.root/'state'
        self.state.mkdir()
        (self.state/'running.json').write_text(json.dumps({'token':'test','startedAt':1}))
        self.env = {**os.environ, 'HOME':str(self.root), 'JIRA_WAKE_DIR':str(self.state),
                    'JIRA_WORKER_CWD':str(self.root)}

    def cli(self, *arguments):
        return subprocess.run([sys.executable,str(SCRIPTS/'drain.py'),'--dir',str(self.state),
            '--token','test',*arguments],capture_output=True,text=True,env=self.env)

    def value(self):
        return {'status':'done','summary':'검증','key':'PROJ-1','stage':'built','waiting':None,'question':None,'suggest':None,
                'checks':[{'name':'runtime','result':'PASS'}],'artifacts':['verify.json']}

    def test_checkpoint_is_present_after_failed_worker_process(self):
        binary=self.root/'bin'
        binary.mkdir()
        stub=binary/'paseo'
        stub.write_text('#!/usr/bin/env python3\nimport json,os,pathlib,sys\n'
            +'value='+repr(self.value())+'\n'
            +'checkpoint=os.environ.get("JIRA_WORKER_CHECKPOINT")\n'
            +'if checkpoint: pathlib.Path(checkpoint).write_text(json.dumps(value))\n'
            +'print(json.dumps(value)); sys.exit(7)\n')
        stub.chmod(0o755)
        self.env['PATH']=str(binary)+os.pathsep+os.environ['PATH']
        brief=self.root/'brief.md'
        brief.write_text('목표: 결과를 쓰고 선택 정리 거부를 보고한다.\n')
        result=subprocess.run(['/bin/bash',str(SCRIPTS/'jira-worker'),'sol','build',str(brief),
            '--dir',str(self.state),'--token','test','--key','PROJ-1'],
            env=self.env,capture_output=True,text=True)
        self.assertEqual(result.returncode,7,result.stderr)
        self.assertEqual(len(list(self.state.rglob('checkpoint.json'))),1,
                         'failed worker must retain the result written before optional cleanup')
        self.assertTrue((self.state/'running.json').exists())

    def test_declared_gate_accepts_complete_evidence_then_blocks_missing_artifact(self):
        result=self.state/'result.json'
        result.write_text(json.dumps(self.value()))
        manifest=self.state/'requirements.json'
        manifest.write_text(json.dumps({'key':'PROJ-1','stage':'built','checks':[
            {'name':'runtime','artifact':'verify.json','resultField':'status','environment':{'environment':'dev'}}]}))
        evidence=self.state/'verify.json'
        evidence.write_text('{"status":"PASS","environment":"dev"}')
        args=('check-result','--key','PROJ-1','--result',str(result),'--requirements',str(manifest))
        accepted=self.cli(*args)
        self.assertEqual(accepted.returncode,0,'complete caller-declared evidence must be accepted: '+accepted.stderr)
        evidence.unlink()
        blocked=self.cli(*args)
        self.assertEqual(blocked.returncode,4,'missing verifier must block declared completion')
        payload=json.loads(blocked.stdout)
        self.assertFalse(payload.get('stageComplete',True))
        self.assertFalse(payload.get('retryAllowed',True))

    def test_legacy_summary_cli_reports_unobserved_usage_as_unknown(self):
        initialized=self.cli('summary-init','--runnable','1','--lane-eligible','0','--now','1')
        self.assertEqual(initialized.returncode,0,initialized.stderr)
        recorded=self.cli('summary-record','--key','PROJ-1','--unit','build','--started-at','1',
                          '--ended-at','3','--workers','1')
        self.assertEqual(recorded.returncode,0,recorded.stderr)
        value=json.loads(recorded.stdout)
        tokens=value.get('metrics',{}).get('tokens',{})
        self.assertEqual(tokens.get('status'),'unknown','absent provider usage must be explicitly unknown')
        self.assertIsNone(tokens.get('total','MISSING'))
        self.assertEqual(tokens.get('unknownUnits'),1)

    def test_scan_public_entrypoint_reports_elapsed_time_without_new_arguments(self):
        output=self.root/'scan.json'
        code=('import sys; from pathlib import Path; sys.path.insert(0,sys.argv[1]); '
              'import followup_scan; followup_scan.main(["--out",sys.argv[2]], '
              'runner=lambda argv: [], base=Path(sys.argv[3]), workspace=Path(sys.argv[3]), sleep=lambda seconds: None)')
        result=subprocess.run([sys.executable,'-c',code,str(SCRIPTS),str(output),str(self.state)],
                              capture_output=True,text=True,env=self.env)
        self.assertEqual(result.returncode,0,result.stderr)
        payload=json.loads(output.read_text())
        self.assertIsInstance(payload.get('timings',{}).get('totalSeconds'),(int,float),
                              'scan must expose measured elapsed time at the existing entrypoint')
