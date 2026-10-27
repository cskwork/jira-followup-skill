"""check_merge_msg.py: promotion merge subject format (2026-10-20 incident)."""
from pathlib import Path
import subprocess, sys, tempfile, unittest

SCRIPT = Path(__file__).resolve().parents[1] / "check_merge_msg.py"


def git(repo, *a):
    subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True)


class MergeMsg(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.r = Path(self.tmp.name)
        git(self.r, "init", "-q", "-b", "stg"); git(self.r, "config", "user.email", "t@x.invalid"); git(self.r, "config", "user.name", "t")
        git(self.r, "commit", "-q", "--allow-empty", "-m", "base")
        git(self.r, "checkout", "-q", "-b", "fix"); git(self.r, "commit", "-q", "--allow-empty", "-m", "fix: 수정 [PROJ-1]")
        git(self.r, "checkout", "-q", "stg")

    def tearDown(self):
        self.tmp.cleanup()

    def check(self, msg):
        git(self.r, "merge", "--no-ff", "fix", "-m", msg)
        rc = subprocess.run([sys.executable, str(SCRIPT), "--repo", str(self.r), "--target", "stg"], capture_output=True).returncode
        git(self.r, "reset", "-q", "--hard", "HEAD~1")
        return rc

    def test_old_format_fails_and_rule_format_passes(self):
        self.assertEqual(self.check("PROJ-1315·PROJ-1389 stg 머지"), 1)
        self.assertEqual(self.check("merge(core-api): 담당자 선택 -> stg [PROJ-1315][PROJ-1389]"), 0)
        self.assertEqual(self.check("merge(core-api): 담당자 선택 -> dev [PROJ-1]"), 1)  # wrong target


if __name__ == "__main__":
    unittest.main()
