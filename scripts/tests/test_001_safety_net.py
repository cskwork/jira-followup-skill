"""시험 실행기가 켠 안전망을 가짜 없이 실제 경로로 확인한다. 이 모듈은 _harness를 import하지 않는다:
실행기(unittest discover의 test_000_harness 또는 pytest의 conftest)가 켜지 않았으면 실패해야 한다.

되돌릴 수 없는 쓰기(실제 Teams 발송, 실제 할 일 추가)를 막기 위해 보호가 켜졌는지 먼저 확인하고, 그다음 실제 호출이
거부되는지 본다(2026-11-10: unittest discover에서 conftest.py가 읽히지 않아 보호가 꺼져 있었다)."""
import os
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import decision_inbox  # noqa: E402
import teams_room  # noqa: E402

REAL_TODO = Path.home() / ".sdlc/todo.md"


def stamp(path):
    return (path.stat().st_mtime_ns, path.stat().st_size) if path.exists() else None


class SafetyNet(unittest.TestCase):
    def test_real_todo_cli_is_refused(self):
        target = os.environ.get("SDLC_TODO")
        self.assertTrue(target, "SDLC_TODO 미설정: 할 일 CLI가 실제 ~/.sdlc/todo.md에 쓴다")
        self.assertFalse(Path(target).parent.is_dir(), f"SDLC_TODO={target}는 쓸 수 있는 경로다")
        before = stamp(REAL_TODO)
        with self.assertRaises(RuntimeError):
            decision_inbox.todo_cli(["add", "[시험] 안전망 확인 — 보이면 시험이 실제 할 일에 쓴 것", "--by", "factory"])
        with self.assertRaises(RuntimeError):
            decision_inbox.todo_cli(["list", "--json"])
        # 경보 경로는 결정 화면이 html이라 할 일을 부르지 않는다.
        self.assertFalse(decision_inbox.alert("[시험] 안전망 확인"))
        self.assertEqual(stamp(REAL_TODO), before)

    def test_real_teams_send_is_refused(self):
        cli = os.environ.get("TEAMS_NATIVE_CLI")
        self.assertTrue(cli, "TEAMS_NATIVE_CLI 미설정: 시험이 실제 채팅 CLI로 보낼 수 있다")
        self.assertFalse(Path(cli).exists(), f"TEAMS_NATIVE_CLI={cli}가 실제로 있다")
        self.assertEqual(teams_room.notify("[시험] 안전망 확인"), {"status": "unknown", "error": "TEAMS_CLI_NOT_FOUND"})

    def test_judge_model_is_off(self):
        self.assertEqual(os.environ.get("JIRA_JUDGE_MODE"), "off")


if __name__ == "__main__":
    unittest.main()
