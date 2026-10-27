"""Offline Git fixtures for optional promotion-tip feature-side checking."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "check_base.py"


class FeatureSideChecks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name)
        self.env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "Verifier Test", "GIT_AUTHOR_EMAIL": "test@example.invalid",
            "GIT_COMMITTER_NAME": "Verifier Test", "GIT_COMMITTER_EMAIL": "test@example.invalid",
            "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
        }
        self.git("init", "-b", "main")
        self.base = self.commit("production baseline")
        self.git("update-ref", "refs/remotes/origin/main", self.base)
        self.git("checkout", "-b", "ticket")

    def git(self, *args):
        result = subprocess.run(
            ["git", "-c", "core.hooksPath=/dev/null", "-C", str(self.repo), *args],
            capture_output=True, text=True, env=self.env, timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def commit(self, subject):
        self.git("commit", "--allow-empty", "-m", subject)
        return self.git("rev-parse", "HEAD")

    def check(self, *args):
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--repo", str(self.repo),
             "--branch", "HEAD", "--key", "PROJ-1388", *args],
            capture_output=True, text=True, env=self.env, timeout=15,
        )
        self.assertEqual(result.stderr, "")
        self.assertEqual(len(result.stdout.splitlines()), 1, result.stdout)
        return result.returncode, json.loads(result.stdout)

    def promote(self):
        self.git("checkout", "-b", "promotion", self.base)
        self.commit("unrelated target change [PROJ-9999]")
        self.git("merge", "--no-ff", "ticket", "-m", "promotion [PROJ-1388]")
        return self.git("rev-parse", "HEAD")

    def test_default_still_checks_and_rejects_full_merge_range(self):
        feature = self.commit("ticket change [PROJ-1388]")
        promotion = self.promote()
        code, data = self.check()
        self.assertEqual((code, data["status"]), (1, "FAIL"))
        self.assertEqual(data["checked_commit"], promotion)
        self.assertEqual(len(data["commits"]), 3)
        self.assertIn(feature, [commit["sha"] for commit in data["commits"]])
        self.assertIn("merge commit", data["commits"][0]["reason"])

    def test_feature_side_checks_only_second_parent(self):
        feature = self.commit("ticket change [PROJ-1388]")
        self.promote()
        code, data = self.check("--feature-side")
        self.assertEqual((code, data["status"]), (0, "PASS"))
        self.assertEqual(data["checked_commit"], feature)
        self.assertEqual([commit["sha"] for commit in data["commits"]], [feature])
        self.assertEqual((data["branch"], data["merge_base"], data["behind"]), ("HEAD", self.base, 0))

    def test_internal_feature_merge_is_still_rejected(self):
        self.commit("ticket change [PROJ-1388]")
        self.git("checkout", "-b", "side", self.base)
        self.commit("ticket side change [PROJ-1388]")
        self.git("checkout", "ticket")
        self.git("merge", "--no-ff", "side", "-m", "internal merge [PROJ-1388]")
        feature = self.git("rev-parse", "HEAD")
        self.promote()
        code, data = self.check("--feature-side")
        self.assertEqual((code, data["status"]), (1, "FAIL"))
        self.assertEqual(data["checked_commit"], feature)
        self.assertIn("merge commit", data["commits"][0]["reason"])

    def test_non_merge_tip_retains_existing_checks(self):
        feature = self.commit("other ticket change [PROJ-1389]")
        normal_code, normal = self.check()
        code, data = self.check("--feature-side")
        self.assertEqual((code, data), (normal_code, normal))
        self.assertEqual((code, data["status"], data["checked_commit"]), (1, "FAIL", feature))
        code, data = self.check("--feature-side", "--also-key", "PROJ-1389")
        self.assertEqual((code, data["status"]), (0, "PASS"))

    def test_root_tip_is_not_substituted(self):
        self.git("checkout", "--detach", self.base)
        code, data = self.check("--feature-side")
        self.assertEqual((code, data["status"], data["checked_commit"]), (0, "PASS", self.base))
        self.assertEqual(data["commits"], [])

    def test_octopus_tip_is_not_substituted(self):
        self.commit("ticket change [PROJ-1388]")
        self.git("checkout", "-b", "side", self.base)
        self.commit("side change [PROJ-1388]")
        self.git("checkout", "-b", "promotion", self.base)
        self.commit("target change [PROJ-1388]")
        self.git("merge", "--no-ff", "ticket", "side", "-m", "octopus [PROJ-1388]")
        octopus = self.git("rev-parse", "HEAD")
        self.assertEqual(len(self.git("show", "-s", "--format=%P", "HEAD").split()), 3)
        code, data = self.check("--feature-side")
        self.assertEqual((code, data["status"], data["checked_commit"]), (1, "FAIL", octopus))
        self.assertEqual(len(data["commits"]), 4)
        self.assertIn("merge commit", data["commits"][0]["reason"])

    def test_behind_count_uses_feature_side_not_promotion_tip(self):
        feature = self.commit("ticket change [PROJ-1388]")
        self.git("checkout", "main")
        production = self.commit("production advanced")
        self.git("update-ref", "refs/remotes/origin/main", production)
        self.git("merge", "--no-ff", "ticket", "-m", "promotion [PROJ-1388]")
        code, data = self.check("--feature-side")
        self.assertEqual((code, data["status"], data["checked_commit"]), (0, "PASS", feature))
        self.assertEqual((data["base_head"], data["merge_base"], data["behind"]), (production, self.base, 1))


if __name__ == "__main__":
    unittest.main()
