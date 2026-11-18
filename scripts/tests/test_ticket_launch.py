"""티켓 launch 명령과 refill 경로는 PATH 스텁으로만 실행한다."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from unittest.mock import patch

import test_run_lock as fixtures


class TicketLaunchTest(fixtures.LivenessFixture):
    def lease(self, **changes):
        data = dict(token='ticket', key='PROJ-1', parent='parent', state='launching',
                    startedAt=time.time(), deadline=time.time() + 9900)
        data.update(changes)
        fixtures.LOCK.atomic_json(self.state / 'tickets/PROJ-1.json', data)

    def launch(self):
        return subprocess.run([sys.executable, str(fixtures.SCRIPTS / 'jira-ticket-run'),
                               '--dir', str(self.state), '--key', 'PROJ-1', '--token', 'ticket',
                               '--parent', 'parent'], env=self.env, capture_output=True, text=True, timeout=15)

    def test_ticket_command_fixed_workspace_parentless_labels_and_single_paragraph(self):
        self.lease()
        self.configure(run_output='ticket-agent')
        self.env.update(JIRA_FACTORY_WORKSPACE='wks_factory', PASEO_AGENT_ID='scheduler',
                        PASEO_WORKSPACE_ID='wks_scheduler')
        result = self.launch()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), 'ticket-agent')
        calls = [row for row in self.recorded() if row['args'][0] == 'run']
        self.assertEqual(len(calls), 1)
        row, args = calls[0], calls[0]['args']
        self.assertIsNone(row['parent'])
        self.assertIsNone(row['workspace'])
        for value in ('--no-wait', 'claude-opus-5-5', 'high', 'bypassPermissions', 'wks_factory',
                      'factory=jira-followup', 'role=ticket-orchestrator', 'run=ticket', 'key=PROJ-1'):
            self.assertIn(value, args)
        prompt = args[-1]
        self.assertIn('TICKET-ORCHESTRATOR.md', prompt)
        self.assertIn('tt=ticket', prompt)
        self.assertIn('parent=parent', prompt)
        self.assertNotIn('\n', prompt)

    def test_ticket_command_carries_min_env_and_off_switch_restores_old_command(self):
        min_env = ['CLAUDE_CODE_DISABLE_AUTO_MEMORY=1', 'HINDSIGHT_DISABLE_HOOKS=1', 'TE_MICROSKILLS=off']
        self.env.update(JIRA_FACTORY_WORKSPACE='wks_factory')
        for switch, expected in ((None, min_env), ('off\n', [])):
            with self.subTest(switch=switch):
                if switch:
                    (self.state / 'min-env').write_text(switch)
                self.lease()
                self.configure(run_output='ticket-agent')
                self.calls.unlink(missing_ok=True)
                result = self.launch()
                self.assertEqual(result.returncode, 0, result.stderr)
                args = [row for row in self.recorded() if row['args'][0] == 'run'][-1]['args']
                self.assertEqual([args[i + 1] for i, a in enumerate(args) if a == '--env'], expected)

    def test_ticket_dry_run_does_not_call_paseo_and_stale_launch_is_denied(self):
        self.lease()
        self.env['JIRA_WAKE_DRY_RUN'] = '1'
        result = self.launch()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(result.stdout.startswith('dry-run: env -u PASEO_AGENT_ID'))
        self.assertEqual(self.recorded(), [])
        (self.state / 'tickets/PROJ-1.json').unlink()
        self.assertEqual(self.launch().returncode, 3)
        self.assertEqual(self.recorded(), [])

    def test_refill_passes_guard_flag_without_manual_or_scheduler_supervisor(self):
        guard = self.root / 'guard'
        args_file = self.root / 'guard-args'
        guard.write_text('#!/bin/bash\nprintf "%s\\n" "$@" >> "$GUARD_ARGS"\necho "jira-daily-guard: skipping duplicate (running)"\n')
        # FOREGROUND: 운영에서는 세션 밖으로 떼어 내는 refill을 시험에서는 동기로 실행해 가드 인자를 바로 읽는다.
        self.env.update(JIRA_WAKE_GUARD=str(guard), GUARD_ARGS=str(args_file), JIRA_WAKE_FOREGROUND='1')
        result = self.cli('jira-daily-wake', '--dir', str(self.state), '--refill')
        self.assertEqual(result.returncode, 0, result.stderr)
        flags = args_file.read_text().splitlines()
        self.assertIn('--refill', flags)
        self.assertNotIn('--manual', flags)
        self.assertFalse((self.state / 'wake-runs').exists())

    def test_busy_tick_checks_six_hour_resource_alert_once(self):
        import resource_lock
        self.lock('parent')
        self.configure(agents=[self.row(token='parent', status='running')])
        resource_lock.atomic_json(self.state / 'locks/runtime-local.json',
            dict(name='runtime-local', token='ticket', key='PROJ-1', attempt='attempt-held',
                 pid=os.getpid(), pidStart='fixture-process-start-' + str(os.getpid()),
                 at=time.time() - 21601))
        with patch.object(resource_lock, 'notify_alert') as notify:
            for _ in range(2):
                self.assertEqual(fixtures.LOCK.main(['guard', '--manual', '--dir', str(self.state)]), 0)
            self.assertEqual(notify.call_count, 1)
        self.assertTrue((self.state / 'locks/runtime-local.json').exists())
