"""idle tick 끝의 sdlc dreaming 실행(dream_tick.py, 사용자 결정 2026-11-16).

jira-daily-wake 전체를 가짜 가드·가짜 paseo(PATH 스텁)·가짜 sdlc.sh(fixtures/fake-sdlc-kit)로만 돌린다."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from _harness import bound

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import dream_tick  # noqa: E402
WAKE = ROOT / "jira-daily-wake"
FAKE_KIT = Path(__file__).resolve().parent / "fixtures/fake-sdlc-kit"
IDLE = "jira-daily-guard: skipping duplicate (idle: events=0, no runnable work)"
MIN_ENV = ["CLAUDE_CODE_DISABLE_AUTO_MEMORY=1", "HINDSIGHT_DISABLE_HOOKS=1", "TE_MICROSKILLS=off"]

# 호출마다 argv를 JSON 한 줄로 남긴다. agent ls는 FAKE_DREAMERS 파일(JSON 목록), run은 dreamer-agent를 낸다.
# FAKE_PASEO_RUN=hang이면 run이 멈춘다(시간 초과), hang-appear면 실행 중 dreamer를 남긴 뒤 멈춘다(느린 성공).
PASEO = """#!/usr/bin/env python3
import json, os, sys, time
with open(os.environ["CALLS"], "a") as stream:
    stream.write(json.dumps(sys.argv[1:]) + "\\n")
if sys.argv[1:3] == ["agent", "ls"]:
    path = os.environ.get("FAKE_DREAMERS", "")
    print(open(path).read() if path and os.path.exists(path) else "[]")
elif sys.argv[1:2] == ["run"]:
    mode = os.environ.get("FAKE_PASEO_RUN", "")
    if mode == "hang-appear":
        with open(os.environ["FAKE_DREAMERS"], "w") as stream:
            json.dump([{"id": "slow-dreamer", "status": "running"}], stream)
    if mode.startswith("hang"):
        time.sleep(30)
    print("dreamer-agent")
"""


class DreamTickTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        bin_dir = self.dir / "bin"
        bin_dir.mkdir()
        (bin_dir / "paseo").write_text(PASEO)
        (bin_dir / "paseo").chmod(0o755)
        (self.dir / "workspace").mkdir()
        self.state = self.dir / ".jira-followup"
        self.state.mkdir(parents=True)
        self.calls = self.dir / "calls.log"
        self.kit_log = self.dir / "kit.log"
        self.env = dict(os.environ, HOME=str(self.dir), JIRA_WAKE_FOREGROUND="1", PATH=f"{bin_dir}:{os.environ['PATH']}",
                        CALLS=str(self.calls), JIRA_WAKE_CWD=str(self.dir / "workspace"), JIRA_FACTORY_WORKSPACE="wks_factory",
                        JIRA_SDLC="on", SDLC_KIT=str(FAKE_KIT), JIRA_SDLC_ROOT=str(self.dir / "workspace"),
                        FAKE_SDLC_LOG=str(self.kit_log), FAKE_SDLC_DREAM="due",
                        FAKE_DREAMERS=str(self.dir / "dreamers.json"), JIRA_DREAM_AT="2026-11-16T10:00:00+09:00")
        self.env.update(JIRA_WAKE_DIR=str(self.state), JIRA_FOLLOWUP_HOME=str(self.state))

    def tearDown(self):
        self.tmp.cleanup()

    def wake(self, guard_output=IDLE, *args, **extra):
        guard = self.dir / "guard"
        guard.write_text(f'#!/bin/bash\n[ "${{1:-}}" = --release ] && exit 0\nprintf "%b\\n" "{guard_output}"\n')
        guard.chmod(0o755)
        env = dict(self.env, JIRA_WAKE_GUARD=str(guard), **extra)
        result = subprocess.run(["/bin/bash", str(WAKE), *args], env=env, capture_output=True, text=True,
                                timeout=bound(30))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def paseo(self, verb=None):
        rows = [json.loads(line) for line in self.calls.read_text().splitlines()] if self.calls.exists() else []
        return [row for row in rows if verb is None or row[:1] == [verb]]

    def kit(self):
        return self.kit_log.read_text().splitlines() if self.kit_log.exists() else []

    def last(self):
        return json.loads((self.state / "dream-last.json").read_text())

    def journal(self):
        return "".join(path.read_text() for path in self.state.glob("followup-*.md"))

    def test_due_launches_one_opus_medium_dreamer_with_min_env_and_label(self):
        self.wake()
        runs = self.paseo("run")
        self.assertEqual(len(runs), 1)
        argv = runs[0]
        for flag, value in (("--provider", "claude"), ("--model", "claude-opus-5-5"), ("--thinking", "medium"),
                            ("--mode", "bypassPermissions"), ("--cwd", str(self.dir / "workspace")),
                            ("--workspace", "wks_factory")):
            self.assertEqual(argv[argv.index(flag) + 1], value)
        self.assertIn("--no-wait", argv)
        self.assertEqual([argv[i + 1] for i, a in enumerate(argv) if a == "--env"], MIN_ENV)
        self.assertEqual([argv[i + 1] for i, a in enumerate(argv) if a == "--label"],
                         ["factory=jira-followup", "role=dreamer"])
        self.assertTrue(argv[-1].startswith("# Role: Dreamer (fake brief)"))
        self.assertEqual(self.kit(), ["dream --check", "dream"])
        record = self.last()
        self.assertEqual((record["outcome"], record["agentId"], record["day"]), ("launched", "dreamer-agent", "2026-11-16"))
        self.assertEqual(record["due"], "due: 3/3 closes since 2026-11-01T00:00:00Z")
        self.assertIn("dream-launched: dreamer-agent", self.journal())

    def test_not_due_launches_nothing(self):
        self.wake(FAKE_SDLC_DREAM="not-due")
        self.assertEqual(self.paseo("run"), [])
        self.assertEqual(self.kit(), ["dream --check"])
        self.assertEqual(self.last()["outcome"], "not-due")
        self.assertEqual(self.last()["due"], "not due: 1/3 closes since 2026-11-01T00:00:00Z")

    def test_checks_once_per_calendar_day(self):
        self.wake(FAKE_SDLC_DREAM="not-due")
        self.wake()  # 같은 날 두 번째 idle tick: due여도 kit·paseo를 다시 부르지 않는다.
        self.assertEqual(self.kit(), ["dream --check"])
        self.assertEqual(self.paseo("run"), [])
        self.wake(JIRA_DREAM_AT="2026-11-17T09:00:00+09:00")
        self.assertEqual(len(self.paseo("run")), 1)
        self.assertEqual(self.last()["day"], "2026-11-17")

    def test_running_dreamer_blocks_launch_and_finished_one_is_archived(self):
        (self.dir / "dreamers.json").write_text(json.dumps([{"id": "old-dreamer", "status": "running"}]))
        self.wake()
        self.assertEqual(self.paseo("run"), [])
        self.assertEqual(self.kit(), [])
        self.assertEqual(self.last()["reason"], "dreamer-running")
        (self.dir / "dreamers.json").write_text(json.dumps([{"id": "old-dreamer", "status": "idle"}]))
        self.wake(JIRA_DREAM_AT="2026-11-17T09:00:00+09:00")
        self.assertEqual(self.paseo("agent")[-1], ["agent", "archive", "old-dreamer"])
        self.assertEqual(len(self.paseo("run")), 1)

    def test_kit_without_dream_skips_with_one_journal_line(self):
        self.wake(FAKE_SDLC_DREAM="missing")
        self.wake(FAKE_SDLC_DREAM="missing")
        self.assertEqual(self.paseo("run"), [])
        self.assertEqual(self.last()["reason"], "kit-without-dream")
        self.assertEqual(self.journal().count("kit-without-dream"), 1)

    def test_kit_error_is_recorded_and_never_launches(self):
        self.wake(FAKE_SDLC_DREAM="error")
        self.assertEqual(self.paseo("run"), [])
        self.assertEqual((self.last()["outcome"], self.last()["reason"]), ("error", "check exit 3"))

    def tick_in_process(self, **extra):
        """LAUNCH_TIMEOUT를 줄이려고 dream_tick.main을 같은 프로세스에서 부른다(가드 없이)."""
        with mock.patch.dict(os.environ, dict(self.env, **extra)), mock.patch.object(dream_tick, "LAUNCH_TIMEOUT", 1):
            dream_tick.main(["--dir", str(self.state)])

    def test_launch_timeout_without_dreamer_keeps_day_and_next_tick_retries(self):
        # 2026-11-17 09:30: 띄우기 시간 초과로 그날 재시도가 없었다.
        self.tick_in_process(FAKE_PASEO_RUN="hang")
        record = self.last()
        self.assertEqual((record["outcome"], record["reason"]), ("error", "launch: TimeoutExpired"))
        self.assertNotIn("day", record)
        self.assertEqual(record["attempts"], {"day": "2026-11-16", "n": 1})
        self.wake()
        self.assertEqual(len(self.paseo("run")), 2)
        record = self.last()
        self.assertEqual((record["outcome"], record["day"], record["attempts"]["n"]), ("launched", "2026-11-16", 2))

    def test_launch_timeout_with_running_dreamer_records_launched(self):
        self.tick_in_process(FAKE_PASEO_RUN="hang-appear")
        record = self.last()
        self.assertEqual((record["outcome"], record["agentId"], record["day"]), ("launched", "slow-dreamer", "2026-11-16"))
        self.assertNotIn(["agent", "archive", "slow-dreamer"], self.paseo("agent"))

    def test_failures_stop_after_three_attempts_a_day(self):
        (self.state / "dream-last.json").write_text(json.dumps({"day": "2026-11-15", "outcome": "not-due"}))
        for _ in range(2):
            self.wake(FAKE_SDLC_DREAM="error")
            self.assertEqual(self.last()["day"], "2026-11-15")  # 앞 날짜 그대로: 오늘은 소비하지 않았다.
        self.wake(FAKE_SDLC_DREAM="error")
        record = self.last()
        self.assertEqual((record["day"], record["attempts"]["n"]), ("2026-11-16", 3))
        self.assertEqual(record["reason"], "check exit 3; attempt cap 3/3 reached, no retry today")
        self.wake(FAKE_SDLC_DREAM="error")
        self.assertEqual(self.kit(), ["dream --check"] * 3)
        self.wake(FAKE_SDLC_DREAM="error", JIRA_DREAM_AT="2026-11-17T09:00:00+09:00")
        self.assertEqual(self.last()["attempts"], {"day": "2026-11-17", "n": 1})

    def test_off_switch_skips_everything(self):
        (self.state / "dream").write_text("off\n")
        result = self.wake()
        self.assertIn("dream: off", result.stdout)
        self.assertEqual((self.paseo(), self.kit()), ([], []))
        self.assertFalse((self.state / "dream-last.json").exists())

    def test_monitoring_unavailable_skip_still_dreams(self):
        # 감시 하나(Teams)가 안 돼도 할 일 없음 판정이면 dreaming은 돈다(2026-11-17: 매 tick이 이 경우였다).
        self.wake("jira-daily-guard: skipping duplicate (monitoring-unavailable: teams-unavailable:X)")
        self.assertEqual(self.last()["outcome"], "launched")

    def test_non_idle_guard_never_dreams(self):
        # 오케스트레이터를 띄운 tick, 실행 중 중복, 수동 실행: dreaming 없음.
        self.wake("run\\nmode=tick")
        self.wake("jira-daily-guard: skipping duplicate (running)")
        self.wake(IDLE, "--now")
        self.assertEqual(self.kit(), [])
        self.assertEqual([argv[argv.index("--label") + 3] for argv in self.paseo("run")], ["role=orchestrator"])
        self.assertFalse((self.state / "dream-last.json").exists())


if __name__ == "__main__":
    unittest.main()
