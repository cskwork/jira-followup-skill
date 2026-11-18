"""2026-11-10 회고: refill 셸이 부른 세션과 함께 죽어 lost-run이 남던 순서를 재현한다.

실측(2026-11-09 15:49 PROJ-1408 티켓 오케스트레이터 d2379b33): `jira-daily-wake --refill`이 30초를 넘겨 배경으로
넘어가고, 세션이 9초 뒤 끝나며 그 task가 `exit 144`로 죽었다. 가드는 15:50:20에 running.json(6733e30a)을 잡았지만
paseo run·attach 전이라 오케스트레이터 없이 잠금만 남았고 16:00 tick이 lost-run으로 회수했다.
"""
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import time
import unittest

import test_run_lock as fixtures

ROOT = Path(__file__).resolve().parents[1]
WAKE = ROOT / "jira-daily-wake"


class RefillSurvivesCallerExit(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.state = self.home / "state"
        self.state.mkdir()
        binary = self.home / "bin"
        binary.mkdir()
        self.calls = self.home / "calls.jsonl"
        (binary / "paseo").write_text('''#!/usr/bin/env python3
import json, os, sys
with open(os.environ["CALLS"], "a") as stream:
    stream.write(json.dumps(sys.argv[1:]) + "\\n")
if sys.argv[1:3] == ["agent", "ls"]:
    print("[]")
elif sys.argv[1] == "run":
    print("new-orchestrator")
''')
        (binary / "paseo").chmod(0o755)
        # 실제 가드처럼 idle 판정에 시간을 쓴 뒤 잠금을 잡고 mode를 출력한다(실측 refill 30초~2분).
        guard = self.home / "guard"
        guard.write_text('''#!/bin/bash
case " $* " in *" --release "*) exit 0 ;; esac
sleep "${GUARD_SLEEP:-0}"
mkdir -p "$JIRA_WAKE_DIR/runs/refilltoken"
printf '{"token": "refilltoken", "startedAt": %s, "mode": "tick", "trigger": "refill", "liveness": "on"}' \\
  "$(date +%s)" > "$JIRA_WAKE_DIR/running.json"
echo "refill run"
echo "mode=tick"
''')
        guard.chmod(0o755)
        self.env = dict(os.environ, HOME=str(self.home), PATH=f"{binary}:{os.environ['PATH']}",
                        CALLS=str(self.calls), JIRA_WAKE_GUARD=str(guard), JIRA_WAKE_CWD=str(self.home),
                        JIRA_WAKE_DIR=str(self.state), JIRA_FACTORY_WORKSPACE="wks_factory", GUARD_SLEEP="2")
        for name in ("JIRA_WAKE_FOREGROUND", "JIRA_WAKE_CHILD", "JIRA_WAKE_DRY_RUN"):
            self.env.pop(name, None)

    def running(self):
        try:
            return json.loads((self.state / "running.json").read_text())
        except (OSError, ValueError):
            return {}

    def test_refill_launches_orchestrator_after_calling_session_is_killed(self):
        caller = subprocess.Popen(["/bin/bash", str(WAKE), "--refill"], env=self.env, start_new_session=True,
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        try:
            caller.wait(timeout=1)
        except subprocess.TimeoutExpired:
            pass
        # 티켓 오케스트레이터 세션이 끝나며 배경 task의 프로세스 묶음을 죽인다([killed]·exit 144).
        try:
            os.killpg(caller.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        caller.wait(timeout=5)
        caller.stdout.close()
        deadline = time.time() + 15
        while time.time() < deadline and not self.running().get("orchestratorId"):
            time.sleep(0.1)
        self.assertEqual(self.running().get("orchestratorId"), "new-orchestrator",
                         "refill이 부른 세션과 함께 죽어 잠금만 남음(lost-run 전 상태)")
        calls = [json.loads(line) for line in self.calls.read_text().splitlines()]
        self.assertIn("run=refilltoken", calls[-1])
        self.assertIn("trigger=refill", calls[-1][-1])
        # refill은 tick 장부를 쓰지 않는다(lost-tick 판정이 refill 시작으로 가려지지 않게).
        self.assertFalse((self.state / "ticks.log").exists())
        # wake는 orchestratorId를 잠금에 붙인 뒤에 이 줄을 쓴다: 위 대기가 끝난 직후에는 아직 없을 수 있다(CI에서 관측).
        path, wanted = next((self.state / "logs").glob("wake-*.log")), "orchestrator=new-orchestrator mode=tick"
        deadline = time.time() + 15
        while time.time() < deadline and wanted not in path.read_text():
            time.sleep(0.1)
        self.assertIn(wanted, path.read_text())


class LostRunLessonNamesLaunchStage(fixtures.LivenessFixture):
    def test_lost_run_lesson_records_missing_orchestrator_and_trigger(self):
        self.lock("refilltoken", startedAt=int(time.time()) - 601, orchestratorId=None, trigger="refill")
        self.configure(agents=[])
        self.assertFalse(self.busy())
        row = json.loads((self.state / "lessons.jsonl").read_text().splitlines()[-1])
        self.assertEqual(row["what"], "lost-run: refilltoken")
        self.assertIn("orchestratorId", row)
        self.assertIsNone(row["orchestratorId"])
        self.assertEqual(row["trigger"], "refill")

    def test_guard_records_trigger_in_running_json(self):
        from unittest.mock import patch
        result = subprocess.CompletedProcess([], 1, "", "")
        with patch.object(fixtures.LOCK.subprocess, "run", return_value=result):
            self.assertEqual(fixtures.LOCK.main(["guard", "--refill", "--dir", str(self.state)]), 0)
        self.assertEqual(fixtures.LOCK.read_lock(self.state)["trigger"], "refill")
        fixtures.LOCK.clear(self.state)
        self.assertEqual(fixtures.LOCK.main(["guard", "--manual", "--dir", str(self.state)]), 0)
        self.assertEqual(fixtures.LOCK.read_lock(self.state)["trigger"], "manual")


if __name__ == "__main__":
    unittest.main()
