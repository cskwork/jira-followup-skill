"""무인 Claude 최소 환경(factory_env.py)과 그 끄기 스위치. 모델·Paseo는 부르지 않는다."""
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import factory_env  # noqa: E402
import judge  # noqa: E402

PAIRS = ["--env", "CLAUDE_CODE_DISABLE_AUTO_MEMORY=1", "--env", "HINDSIGHT_DISABLE_HOOKS=1",
         "--env", "TE_MICROSKILLS=off"]


class SwitchChecks(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def test_default_on_and_only_off_turns_it_off(self):
        self.assertEqual(factory_env.paseo_args(self.dir), PAIRS)
        for value, expected in (("on\n", PAIRS), ("", PAIRS), ("garbage", PAIRS), ("off\n", []), ("OFF", [])):
            with self.subTest(value=value):
                (self.dir / "min-env").write_text(value)
                self.assertEqual(factory_env.paseo_args(self.dir), expected)

    def test_profile_never_drops_setting_sources_or_hooks(self):
        # 안전 훅은 설정 출처에서 실린다. 최소 환경은 설정 출처·훅 전체를 끄는 옵션을 쓰지 않는다.
        flat = " ".join(factory_env.paseo_args(self.dir) + factory_env.JUDGE_FLAGS)
        for forbidden in ("--setting-sources", "--bare", "disableAllHooks", "CLAUDE_CONFIG_DIR",
                          "CLAUDE_CODE_DISABLE_CLAUDE_MDS", "TE_DISABLE", "BLAST_RADIUS"):
            self.assertNotIn(forbidden, flat)

    def test_judge_ask_uses_profile_and_off_restores_old_command(self):
        seen = []

        def runner(command, **kwargs):
            seen.append((command, kwargs.get("env")))
            return SimpleNamespace(returncode=1, stdout="{}", stderr="")

        with patch.dict(os.environ, {"JIRA_WAKE_DIR": str(self.dir)}):
            judge.ask({}, runner=runner)
            (self.dir / "min-env").write_text("off\n")
            judge.ask({}, runner=runner)
        (on_command, on_env), (off_command, off_env) = seen
        self.assertEqual(on_command[:len(judge.MODEL)], judge.MODEL)
        self.assertEqual(on_command[len(judge.MODEL):len(judge.MODEL) + len(factory_env.JUDGE_FLAGS)],
                         factory_env.JUDGE_FLAGS)
        self.assertEqual(on_env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"], "1")
        self.assertEqual(on_env["HINDSIGHT_DISABLE_HOOKS"], "1")
        self.assertEqual(off_command[:len(judge.MODEL)], judge.MODEL)
        self.assertEqual(off_command[len(judge.MODEL)], "--json-schema")
        self.assertIsNone(off_env)


if __name__ == "__main__":
    unittest.main()
