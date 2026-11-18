"""Worker prompts use only fixed local policy documents; paseo is a stub."""
import json
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from _harness import bound  # 부하에 맞춘 하위 프로세스 한도

SCRIPTS = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("runbook_section", SCRIPTS / "runbook_section.py")
SECTION = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(SECTION)


class WorkerSectionChecks(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.skill = self.root / "skill with spaces"
        self.scripts = self.skill / "scripts"
        self.scripts.mkdir(parents=True)
        for name in ("jira-worker", "runbook_section.py", "run_lock.py", "followup_config.py", "worker-result.schema.json",
                     "worker_runtime.py", "requirements.py", "lifecycle.py", "state_update.py", "factory_workspace.py",
                     "factory_hold.py", "factory_env.py"):
            if (SCRIPTS / name).exists():
                shutil.copy2(SCRIPTS / name, self.scripts / name)
        self.runbook = self.skill / "RUNBOOK.md"
        self.runbook.write_text(
            "# Runbook\n## 0. Authority\nauthority-body\n" +
            "".join(f"## {number}. Morning\nmorning-{number}-body\n" for number in range(1, 7)) +
            "## 7. Work\nwork-intro\n### 7-3. Ticket\nticket-body\n" +
            "".join(f"{number}. **Step**: work-{number}\n" for number in range(7)) +
            "### 7-3-C. Conflict\nconflict-body\n### 7-4. Other\nother-body\n")
        (self.skill / "ORCHESTRATOR.md").write_text(
            "# Orchestrator\n## 0. Boundary\norchestrator-boundary\n"
            "## 2. Assignment\norchestrator-assignment\n## 3. Brief\norchestrator-brief\n")
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.capture = self.root / "paseo.json"
        paseo = self.bin / "paseo"
        paseo.write_text("#!/usr/bin/env python3\nimport json, os, pathlib, sys\n"
                         "if sys.argv[1:3] == ['agent', 'ls']: print('[]'); sys.exit(0)\n"
                         "pathlib.Path(os.environ['CAPTURE']).write_text(json.dumps(sys.argv[1:]))\n"
                         "print('stub-worker')\nsys.exit(int(os.environ.get('PASEO_RC', '0')))\n")
        paseo.chmod(0o755)
        state = self.root / ".jira-followup"
        state.mkdir(parents=True)
        (state / ".running-fixture").write_text(json.dumps({"token": "fixture"}))
        os.replace(state / ".running-fixture", state / "running.json")
        self.env = dict(os.environ, HOME=str(self.root), JIRA_WAKE_DIR=str(state), PATH=f"{self.bin}:{os.environ['PATH']}",
                        CAPTURE=str(self.capture), JIRA_WORKER_CWD=str(self.root),
                        JIRA_FACTORY_WORKSPACE="wks_factory")

    def tearDown(self):
        self.tmp.cleanup()

    def worker(self, brief, *extra, role="sol"):
        path = self.root / "brief.md"
        path.write_text(brief)
        return subprocess.run(["/bin/bash", str(self.scripts / "jira-worker"),
                               role, "PROJ-1 unit", str(path), *extra, "--token", "fixture"],
                              env=self.env, capture_output=True, text=True, timeout=bound(10))

    def prompt(self):
        return json.loads(self.capture.read_text())[-1]

    def test_both_roles_are_unattended(self):
        for role in ('sol', 'opus'):
            with self.subTest(role=role):
                result = self.worker('목표: 시험\n', role=role)
                self.assertEqual(result.returncode, 0, result.stderr)
                argv = json.loads(self.capture.read_text())
                self.assertIn('--env', argv)
                self.assertEqual(argv[argv.index('--env') + 1], 'BLAST_RADIUS_UNATTENDED=1')

    def test_opus_gets_min_env_sol_does_not_and_off_switch_restores_old_command(self):
        min_env = ['BLAST_RADIUS_UNATTENDED=1', 'CLAUDE_CODE_DISABLE_AUTO_MEMORY=1', 'HINDSIGHT_DISABLE_HOOKS=1',
                   'TE_MICROSKILLS=off']
        state = Path(self.env['JIRA_WAKE_DIR'])
        for role, switch, expected in (('opus', None, min_env), ('sol', None, min_env[:1]),
                                       ('opus', 'off\n', min_env[:1])):
            with self.subTest(role=role, switch=switch):
                if switch:
                    (state / 'min-env').write_text(switch)
                result = self.worker('목표: 시험\n', role=role)
                self.assertEqual(result.returncode, 0, result.stderr)
                argv = json.loads(self.capture.read_text())
                self.assertEqual([argv[i + 1] for i, a in enumerate(argv) if a == '--env'], expected)

    def test_model_capacity_is_reported_as_rate_limit(self):
        result = self.worker('목표: 시험\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('429, overloaded or model at capacity', self.prompt())

    def cancellation_stub(self, *, status='running', stop_fails=False, stays_running=False):
        self.env.update(AGENT_STATUS=status, STOP_FAILS=str(int(stop_fails)),
                        STAYS_RUNNING=str(int(stays_running)))
        (self.bin / 'paseo').write_text('''#!/usr/bin/env python3
import json, os, pathlib, sys
args = sys.argv[1:]
root = pathlib.Path(os.environ['CAPTURE']).parent
with (root / 'calls.jsonl').open('a') as stream:
    stream.write(json.dumps(args) + '\\n')
state = root / 'agent-status'
status = state.read_text() if state.exists() else os.environ['AGENT_STATUS']
if args[0] == 'run':
    pathlib.Path(os.environ['CAPTURE']).write_text(json.dumps(args))
    if os.environ.get('VALID_OUTPUT'):
        print(json.dumps({'status':'done','summary':'검증','key':'PROJ-1','stage':'built',
                          'waiting':None,'question':None,'suggest':None,'artifacts':[],'checks':[]}))
    else:
        print('OUTPUT_SCHEMA_FAILED')
    sys.exit(int(os.environ.get('PASEO_RC', '1')))
elif args[:2] == ['agent', 'ls']:
    if any(name in os.environ for name in ('PASEO_AGENT_ID', 'PASEO_WORKSPACE_ID')):
        print('query inherited caller identity', file=sys.stderr)
        sys.exit(9)
    if os.environ.get('QUERY_FAILS'):
        print('fixture query rejected', file=sys.stderr)
        sys.exit(8)
    print(json.dumps([{'id': 'worker-full-id', 'status': status}]))
elif args[0] == 'stop':
    if os.environ['STOP_FAILS'] == '1':
        print('fixture stop rejected', file=sys.stderr)
        sys.exit(7)
    if os.environ['STAYS_RUNNING'] != '1':
        state.write_text('idle')
elif args[0] != 'wait':
    sys.exit(9)
''')

    def cancellation_result(self):
        response = self.worker('목표: 시험\n')
        receipt = json.loads(next(Path(self.env['JIRA_WAKE_DIR']).glob('runs/fixture/attempt-*/worker.json')).read_text())
        calls = [json.loads(row) for row in (self.root / 'calls.jsonl').read_text().splitlines()]
        return response, receipt, calls

    def test_schema_failure_cancels_and_confirms_only_this_attempt(self):
        self.cancellation_stub()
        self.env.update(PASEO_AGENT_ID='parent', PASEO_WORKSPACE_ID='parent-workspace')
        response, receipt, calls = self.cancellation_result()
        self.assertEqual(response.returncode, 1, response.stderr)
        self.assertEqual(response.stdout, 'OUTPUT_SCHEMA_FAILED\n')
        self.assertIn(['stop', 'worker-full-id', '--json'], calls)
        self.assertIn('cancelled: worker-full-id', receipt.get('summary', ''))
        queries = [row for row in calls if row[:2] == ['agent', 'ls']]
        self.assertGreaterEqual(len(queries), 2)
        run = next(row for row in calls if row[0] == 'run')
        attempt = next(v for v in run if v.startswith('attempt='))
        for query in queries:
            self.assertIn(attempt, query)
            self.assertIn('factory=jira-followup', query)
            self.assertIn('run=fixture', query)

    def test_finished_agent_is_never_cancelled(self):
        self.cancellation_stub(status='idle')
        response, receipt, calls = self.cancellation_result()
        self.assertEqual(response.returncode, 1)
        self.assertFalse(any(row[0] == 'stop' for row in calls))
        self.assertEqual(receipt.get('cancelledAgents'), [])

    def test_failed_cancel_is_reported_without_changing_exit_code(self):
        self.cancellation_stub(stop_fails=True)
        response, receipt, calls = self.cancellation_result()
        self.assertEqual(response.returncode, 1)
        self.assertIn('fixture stop rejected', response.stderr)
        self.assertIn('cancel failed: worker-full-id', receipt.get('summary', ''))
        self.assertTrue(receipt.get('cancellationFailed'))
        self.assertEqual(receipt.get('cancelledAgents'), [])

    def test_stop_acknowledgment_without_idle_is_failure(self):
        self.cancellation_stub(stays_running=True)
        response, receipt, calls = self.cancellation_result()
        self.assertEqual(response.returncode, 1)
        self.assertIn('still running', response.stderr)
        self.assertTrue(receipt.get('cancellationFailed'))
        self.assertNotIn('cancelled: worker-full-id', receipt.get('summary', ''))

    def test_wait_timeout_also_cancels(self):
        self.cancellation_stub()
        self.env['PASEO_RC'] = '124'
        response, receipt, calls = self.cancellation_result()
        self.assertEqual(response.returncode, 124)
        self.assertEqual(receipt['outcome'], 'timeout')
        self.assertIn('cancelled: worker-full-id', receipt.get('summary', ''))

    def test_zero_exit_with_invalid_output_still_cancels(self):
        self.cancellation_stub()
        self.env['PASEO_RC'] = '0'
        response, receipt, calls = self.cancellation_result()
        self.assertEqual(response.returncode, 0)
        self.assertFalse(receipt['resultValid'])
        self.assertIn('cancelled: worker-full-id', receipt.get('summary', ''))

    def test_nonzero_exit_with_valid_output_still_cancels(self):
        self.cancellation_stub()
        self.env.update(PASEO_RC='7', VALID_OUTPUT='1')
        response, receipt, calls = self.cancellation_result()
        self.assertEqual(response.returncode, 7)
        self.assertTrue(receipt['resultValid'])
        self.assertIn('cancelled: worker-full-id', receipt.get('summary', ''))

    def test_valid_success_needs_no_cancel_or_query(self):
        self.cancellation_stub()
        self.env.update(PASEO_RC='0', VALID_OUTPUT='1')
        response, receipt, calls = self.cancellation_result()
        self.assertEqual(response.returncode, 0)
        self.assertTrue(receipt['stageComplete'])
        self.assertEqual(len(calls), 1)

    def test_query_failure_is_reported_without_unscoped_stop(self):
        self.cancellation_stub()
        self.env['QUERY_FAILS'] = '1'
        response, receipt, calls = self.cancellation_result()
        self.assertEqual(response.returncode, 1)
        self.assertIn('fixture query rejected', response.stderr)
        self.assertTrue(receipt['cancellationFailed'])
        self.assertIn('cancel failed: attempt=', receipt['summary'])
        self.assertFalse(any(row[0] == 'stop' for row in calls))

    def test_worker_runs_in_factory_workspace(self):
        result = self.worker("목표: 시험\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        argv = json.loads(self.capture.read_text())
        self.assertEqual(argv[argv.index("--workspace") + 1], "wks_factory")

    def test_declaration_errors_refuse_before_attempt(self):
        manifest = self.root / "requirements.json"
        manifest.write_text(json.dumps({"key": "PROJ-1", "stage": "plan", "checks": [
            {"name": "review", "artifact": "review.json", "resultField": "status"}]}))
        result = self.worker("목표: 시험\nORCHESTRATOR §7-6\n", "--key", "PROJ-1", "--requirements", str(manifest))
        self.assertEqual(result.returncode, 2)
        self.assertIn("brief: ORCHESTRATOR.md: missing section", result.stderr)
        self.assertIn("environment(비지 않은 객체)", result.stderr)
        self.assertFalse(self.capture.exists())
        self.assertFalse(list((self.root / ".jira-followup/runs").glob("*/attempt-*")))

    def test_authority_is_included_without_citations(self):
        result = self.worker("목표: 시험\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("authority-body", self.prompt())
        self.assertNotIn("ticket-body", self.prompt())
        self.assertNotIn("morning-1-body", self.prompt())

    def test_hook_denial_is_reported_not_bypassed(self):
        # 2026-10-23 PROJ-1379: 정리 단계 훅 거부를 unlink로 우회하려다 확인 질문에 멈춰 결과 JSON 없이 종료
        result = self.worker("목표: 시험\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        prompt = self.prompt()
        self.assertIn("never retry it with another command", prompt)
        # 2026-11-02 PROJ-1469·1470: 정리 확인 질문에 멈춰 결과를 못 냈다. 작업자는 정리하지 않고 대상만 적는다.
        self.assertIn("Never remove worktrees or delete merge/* branches yourself", prompt)
        self.assertIn("cleanup:<absolute worktree path>", prompt)
        # 2026-10-29 PROJ-1431: 본문 없는 커밋을 commit-msg 훅이 거부했다.
        self.assertIn("git commit -m '<title>' -m '<body>'", prompt)

    def structured_stub(self, *, waive_manifest=False):
        (self.bin / "paseo").write_text("#!/usr/bin/env python3\nimport json, os, pathlib, sys\n"
            + "if sys.argv[1:3] == ['agent', 'ls']: print('[]'); sys.exit(0)\n"
            + "value={'status':'done','summary':'검증','key':'PROJ-1','stage':'built','waiting':None,'question':None,'suggest':None,'artifacts':['verify.json'],'checks':[{'name':'runtime','result':'PASS'}]}\n"
            + "pathlib.Path(os.environ['JIRA_WORKER_CHECKPOINT']).write_text(json.dumps(value))\n"
            + ("pathlib.Path(os.environ['MANIFEST']).write_text('{\"key\":\"PROJ-1\",\"stage\":\"built\",\"checks\":[]}')\n" if waive_manifest else "")
            + "print(json.dumps(value)); sys.exit(int(os.environ.get('PASEO_RC','0')))\n")

    def test_wrapper_preserves_unique_checkpoint_and_failure_receipt(self):
        self.structured_stub()
        self.env['PASEO_RC'] = '7'
        for _ in range(2):
            result = self.worker("목표: 시험\n", '--key','PROJ-1')
            self.assertEqual(result.returncode,7,result.stderr)
        state = Path(self.env['JIRA_WAKE_DIR'])
        attempts = list((state/'runs/fixture').glob('attempt-*'))
        self.assertEqual(len(attempts),2)
        for attempt in attempts:
            receipt=json.loads((attempt/'worker.json').read_text())
            self.assertTrue(receipt['checkpointValid'])
            self.assertFalse(receipt['stageComplete'])
            self.assertEqual(receipt['exitCode'],7)
        self.assertTrue((state/'running.json').exists())

    def test_wrapper_snapshots_requirements_before_worker_can_waive_them(self):
        self.structured_stub(waive_manifest=True)
        manifest=self.root/'requirements.json'
        manifest.write_text(json.dumps({'key':'PROJ-1','stage':'built','checks':[
            {'name':'runtime','artifact':'verify.json','resultField':'status','environment':{'environment':'dev'}}]}))
        (self.root/'verify.json').write_text(json.dumps({'status':'PASS','environment':'stg'}))
        self.env['MANIFEST']=str(manifest)
        result=self.worker("목표: 시험\n",'--key','PROJ-1','--requirements',str(manifest))
        self.assertEqual(result.returncode,4,result.stderr)
        gate=json.loads(next(Path(self.env['JIRA_WAKE_DIR']).glob('runs/fixture/attempt-*/gate.json')).read_text())
        self.assertFalse(gate['passed'])
        self.assertEqual(gate['failures'][0]['reason'],'environment-mismatch')

    def test_ticket_and_conflict_citations_append_only_those_sections(self):
        result = self.worker("목표: 충돌 해결\nRUNBOOK §7-3, §7-3-C, §7-3\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        prompt = self.prompt()
        for text in ("authority-body", "ticket-body", "conflict-body"):
            self.assertEqual(prompt.count(text), 1)
        self.assertNotIn("other-body", prompt)
        self.assertNotIn("morning-1-body", prompt)
        self.assertIn("목표: 충돌 해결", prompt)

    def test_morning_range_expands_all_six_sections(self):
        result = self.worker("RUNBOOK §1~§6\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        for number in range(1, 7):
            self.assertIn(f"morning-{number}-body", self.prompt())
        self.assertNotIn("ticket-body", self.prompt())

    def test_explicit_orchestrator_reference_uses_that_document(self):
        result = self.worker("RUNBOOK §7-3; `ORCHESTRATOR.md` §2\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        prompt = self.prompt()
        self.assertIn("authority-body", prompt)
        self.assertIn("ticket-body", prompt)
        self.assertIn("orchestrator-assignment", prompt)
        self.assertNotIn("morning-2-body", prompt)
        self.assertNotIn("orchestrator-brief", prompt)

    def test_brief_paths_are_data_and_never_read(self):
        secret = self.root / "secret.md"
        secret.write_text("SHOULD-NOT-BE-READ")
        result = self.worker(f"RUNBOOK §7-3\n참고: {secret}\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("SHOULD-NOT-BE-READ", self.prompt())

    def test_missing_requested_section_prevents_launch(self):
        result = self.worker("RUNBOOK §7-99\n")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("7-99", result.stderr)
        self.assertFalse(self.capture.exists())

    def test_numbered_step_citations_include_their_parent_once(self):
        result = self.worker("RUNBOOK §7-3-0, §7-3-2, §7-3-6, §7-3\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.prompt().count("ticket-body"), 1)
        self.assertNotIn("conflict-body", self.prompt())

    def test_unknown_numbered_step_prevents_launch(self):
        result = self.worker("RUNBOOK §7-3-99\n")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("7-3-99", result.stderr)
        self.assertFalse(self.capture.exists())

    def test_actual_orchestrator_citations_resolve_against_runbook(self):
        skill = SCRIPTS.parent
        brief = (skill / "ORCHESTRATOR.md").read_text()
        prompt = SECTION.worker_sections(skill, brief)
        self.assertEqual(prompt.count("## 0. 모드"), 1)
        self.assertEqual(prompt.count("### 7-3. 작업 진행"), 1)
        self.assertEqual(prompt.count("### 7-3-C. 병합 충돌 해결"), 1)

    def test_missing_authority_prevents_launch(self):
        self.runbook.write_text("## 1. Only\nno-authority\n")
        result = self.worker("목표: 시험\n")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("0", result.stderr)
        self.assertFalse(self.capture.exists())

    def test_missing_orchestrator_section_prevents_launch(self):
        result = self.worker("ORCHESTRATOR §9\n")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("9", result.stderr)
        self.assertFalse(self.capture.exists())

    def test_missing_policy_file_prevents_launch(self):
        self.runbook.unlink()
        result = self.worker("목표: 시험\n")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("RUNBOOK.md", result.stderr)
        self.assertFalse(self.capture.exists())

    def test_invalid_range_prevents_launch(self):
        for citation in ("§6~§1", "§0~§999999999"):
            with self.subTest(citation=citation):
                result = self.worker(f"RUNBOOK {citation}\n")
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(self.capture.exists())

    def test_timeout_cwd_and_worker_failure_are_preserved(self):
        self.env["PASEO_RC"] = "3"
        result = self.worker("RUNBOOK §7-3\n", "120m")
        self.assertEqual(result.returncode, 3, result.stderr)
        argv = json.loads(self.capture.read_text())
        self.assertEqual(argv[argv.index("--cwd") + 1], str(self.root))
        self.assertEqual(argv[argv.index("--wait-timeout") + 1], "120m")


if __name__ == "__main__":
    unittest.main()
