"""잠금 중 수동·결정 페이지 요청은 외부 호출 없이 pending focus에 보존한다."""
from datetime import date
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class PendingFocusWakeTests(unittest.TestCase):
    def test_locked_manual_focus_is_queued_in_the_selected_state_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            guard = directory / 'guard'
            guard.write_text('#!/bin/bash\necho "jira-daily-guard: skipping duplicate (running)"\n')
            state = directory / 'custom state'
            env = dict(os.environ, JIRA_WAKE_GUARD=str(guard))
            for _ in range(2):
                result = subprocess.run(['/bin/bash', str(ROOT / 'jira-daily-wake'), '--now',
                                         '--key', 'PROJ-1404', '--dir', str(state)],
                                        capture_output=True, text=True, env=env, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.strip(), 'jira-daily-guard: skipping duplicate (running)')
            rows = [json.loads(line) for line in (state / 'pending-focus.jsonl').read_text().splitlines()]
            self.assertEqual(len(rows), 2)
            self.assertTrue(all(row['key'] == 'PROJ-1404' and row['trigger'] == 'manual' for row in rows))

    def test_idle_skip_does_not_enqueue_an_unrelated_focus(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            guard = directory / 'guard'
            guard.write_text('#!/bin/bash\necho "jira-daily-guard: skipping duplicate (idle: events=0, no runnable work)"\n')
            result = subprocess.run(['/bin/bash', str(ROOT / 'jira-daily-wake'), '--now',
                                     '--key', 'PROJ-1404', '--dir', str(directory)],
                                    capture_output=True, text=True,
                                    env=dict(os.environ, JIRA_WAKE_GUARD=str(guard)), timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse((directory / 'pending-focus.jsonl').exists())

    def test_real_guard_wakes_on_pending_focus_with_no_events_or_queue(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            binary = directory / '.local/bin'
            binary.mkdir(parents=True)
            once = binary / 'paseo-once'
            once.write_text('#!/bin/sh\necho run\n')
            once.chmod(0o755)
            state = directory / 'state'
            state.mkdir()
            (state / 'done-today').write_text(date.today().isoformat())
            (state / 'pending-focus.jsonl').write_text('{"key":"PROJ-1404"}\n')
            scan = directory / 'scan.py'
            scan.write_text('import pathlib,sys\npathlib.Path(sys.argv[-1]).write_text(\'{"events":[],"errors":[]}\')\n')
            result = subprocess.run(['/bin/bash', str(ROOT / 'jira-daily-guard'), '--dir', str(state)],
                                    env=dict(os.environ, HOME=str(directory),
                                             FOLLOWUP_IDLE_SCAN_CMD=shlex.join([sys.executable, str(scan)])),
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('mode=tick', result.stdout)
            self.assertTrue((state / 'running.json').exists())
            self.assertIn('pending-focus', json.loads((state / 'guard-last.json').read_text())['reasons'])
