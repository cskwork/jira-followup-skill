"""Lesson injection uses real launchers and offline Paseo/guard fixtures only."""
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import time

from _harness import bound
from test_run_lock import LivenessFixture, LOCK, SCRIPTS


HEADING = '최근 14일 교훈(같은 실수 반복 금지):'


class LessonPromptLaunches(LivenessFixture):
    def setUp(self):
        super().setUp()
        self.skill = self.root / 'skill'
        self.scripts = self.skill / 'scripts'
        self.scripts.mkdir(parents=True)
        copied = {'jira-daily-wake', 'jira-ticket-run', 'jira-worker', 'lesson_prompt.py'}
        for path in SCRIPTS.iterdir():
            if path.is_file():
                target = self.scripts / path.name
                if path.name in copied:
                    shutil.copy2(path, target)
                else:
                    target.symlink_to(path)
        for path in SCRIPTS.parent.iterdir():
            if path.is_file():
                (self.skill / path.name).symlink_to(path)
        self.env.update(JIRA_FACTORY_WORKSPACE='wks_factory', JIRA_WAKE_FOREGROUND='1')
        self.brief = self.root / 'brief.md'
        self.brief.write_text('목표: prompt 확인\nstage=build\n')
        guard = self.root / 'guard'
        guard.write_text('#!/bin/bash\necho mode=tick\n')
        self.env['JIRA_WAKE_GUARD'] = str(guard)
        self.lock('parent')
        LOCK.atomic_json(self.state / 'tickets/PROJ-1.json',
            dict(token='ticket', key='PROJ-1', parent='parent', state='launching',
                 startedAt=time.time(), deadline=time.time() + 9900))

    def lessons(self):
        at = datetime.now(timezone.utc).isoformat()
        rows = [dict(at=at, kind=kind, stage=stage, **{'class': cls},
                     what=what, suggestion=what, evidence=['fixture.json'])
                for kind, stage, cls, what in [
                    ('infra_fail', 'build', 'requirements', 'BUILD-INFRA-ONLY'),
                    ('worker_fail', 'build', 'brief', 'BUILD-WORKER-ONLY'),
                    ('infra_fail', 'stg-deployed', 'deploy', 'DEPLOY-ONLY')]]
        (self.state / 'lessons.jsonl').write_text(
            ''.join(json.dumps(row) + '\n' for row in rows))

    def launch(self, launcher, *extra, worker_output=None):
        if launcher == 'jira-ticket-run':
            self.configure(run_output='ticket-agent')
            args = ['--dir', str(self.state), '--key', 'PROJ-1', '--token', 'ticket', '--parent', 'parent']
            interpreter = sys.executable
        elif launcher == 'jira-worker':
            self.configure(run_output=worker_output or '{"status":"done","stage":"build"}')
            args = ['sol', 'prompt', str(self.brief), '--dir', str(self.state), '--token', 'parent']
            interpreter = '/bin/bash'
        else:
            self.configure(run_output='global-agent')
            args = ['--now', '--dir', str(self.state)]
            interpreter = '/bin/bash'
        self.calls.unlink(missing_ok=True)
        result = subprocess.run([interpreter, str(self.scripts / launcher), *args, *extra],
            env=self.env, capture_output=True, text=True, timeout=bound(25))
        calls = [row for row in self.recorded() if row['args'][0] == 'run']
        self.assertEqual(len(calls), 1, result.stdout + result.stderr)
        return result, calls[0]['args'][-1]

    def test_global_and_ticket_receive_full_index_and_state_dir_with_spaces(self):
        self.lessons()
        for launcher in ('jira-daily-wake', 'jira-ticket-run'):
            with self.subTest(launcher=launcher):
                result, prompt = self.launch(launcher)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(HEADING, prompt)
                for marker in ('BUILD-INFRA-ONLY', 'BUILD-WORKER-ONLY', 'DEPLOY-ONLY'):
                    self.assertIn(marker, prompt)
                self.assertIn(str(self.state), prompt)
        self.assertFalse((self.state / 'lessons-index.md').exists())

    def test_worker_filters_explicit_brief_stage_and_kind_between_sections_and_brief(self):
        self.lessons()
        self.brief.write_text('목표: prompt 확인\nstage: build\nkind=infra_fail\n')
        result, prompt = self.launch('jira-worker', '--key', 'PROJ-1')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('BUILD-INFRA-ONLY', prompt)
        self.assertNotIn('BUILD-WORKER-ONLY', prompt)
        self.assertNotIn('DEPLOY-ONLY', prompt)
        self.assertLess(prompt.index('## 0.'), prompt.index(HEADING))
        self.assertLess(prompt.index(HEADING), prompt.index('--- BRIEF ---'))
        # 일지·상태 쓰기 규칙은 RUNBOOK §0-1 한 곳에 있고(§0은 모든 작업자 프롬프트에 실린다), 머리말은 가리키기만 한다.
        self.assertIn('Follow §0-1 for shell variables, state/queue/journal/note writes', prompt)
        self.assertNotIn('Journal and lessons allow', prompt)
        self.assertIn('lesson --json', prompt)
        self.assertIn('"class":', prompt)
        self.assertIn('"evidence": [', prompt)
        self.assertIn('lesson_classes.json', prompt)
        sample = re.search(r"lesson --json ('[^']*')", prompt).group(1)
        payload = json.loads(shlex.split(sample)[0])
        self.assertEqual(payload['key'], 'PROJ-1')
        self.assertIsInstance(payload['evidence'], list)
        self.assertIn('suggestion', payload)

    def test_requirements_stage_overrides_brief_and_never_uses_worker_role_as_kind(self):
        self.lessons()
        artifact = self.root / 'gate-evidence.json'
        artifact.write_text(json.dumps({'result': 'PASS', 'env': 'offline'}))
        manifest = self.root / 'requirements.json'
        manifest.write_text(json.dumps(dict(key='PROJ-1', stage='stg-deployed', checks=[
            dict(name='offline', artifact=str(artifact), resultField='result', environment={'env': 'offline'})])))
        output = json.dumps(dict(status='done', summary='fixture', key='PROJ-1',
            stage='stg-deployed', waiting=None, artifacts=[str(artifact)],
            checks=[{'name': 'offline', 'result': 'PASS'}],
            question=None, suggest=None, humanAction=None))
        result, prompt = self.launch('jira-worker', '--requirements', str(manifest), '--key', 'PROJ-1',
                                    worker_output=output)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('DEPLOY-ONLY', prompt)
        self.assertNotIn('BUILD-INFRA-ONLY', prompt)
        self.assertNotIn('BUILD-WORKER-ONLY', prompt)

    def test_unrelated_decision_kind_does_not_filter_out_stage_lessons(self):
        self.lessons()
        self.brief.write_text('stage=build\n{"kind":"choice"}\n')
        result, prompt = self.launch('jira-worker')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('BUILD-INFRA-ONLY', prompt)
        self.assertIn('BUILD-WORKER-ONLY', prompt)
        self.assertNotIn('DEPLOY-ONLY', prompt)

    def test_missing_or_ambiguous_worker_stage_skips_index_and_still_launches(self):
        self.lessons()
        for text in ('목표: build 작업\n', 'stage=build\nstage=dev-deployed\n'):
            with self.subTest(brief=text):
                self.brief.write_text(text)
                result, prompt = self.launch('jira-worker')
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertNotIn(HEADING, prompt)
                self.assertNotIn('BUILD-INFRA-ONLY', prompt)
                self.assertIn('launching without lesson index', result.stderr)

    def test_index_error_discards_partial_stdout_and_all_three_launchers_continue(self):
        stub = self.scripts / 'lessons.py'
        stub.unlink()
        stub.write_text('import sys\nif __name__ == "__main__":\n'
                        '    print("PARTIAL-INDEX-MUST-NOT-LEAK")\n'
                        '    print("fixture index failure", file=sys.stderr)\n'
                        '    sys.exit(8)\n')
        for launcher in ('jira-daily-wake', 'jira-ticket-run', 'jira-worker'):
            with self.subTest(launcher=launcher):
                result, prompt = self.launch(launcher)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertNotIn(HEADING, prompt)
                self.assertNotIn('PARTIAL-INDEX-MUST-NOT-LEAK', prompt)
                self.assertIn('launching without lesson index', result.stderr)

    def test_empty_index_has_no_heading(self):
        for launcher in ('jira-daily-wake', 'jira-ticket-run', 'jira-worker'):
            with self.subTest(launcher=launcher):
                result, prompt = self.launch(launcher)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertNotIn(HEADING, prompt)
