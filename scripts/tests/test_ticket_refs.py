"""Offline tests for ticket_refs.py with a throwaway bare origin."""

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import ticket_refs  # noqa: E402


def git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


class TicketRefs(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.origin, self.repo = base / "origin.git", base / "core-api"
        subprocess.run(["git", "init", "-q", "--bare", str(self.origin)], check=True)
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.repo)], check=True)
        for key, value in (("user.name", "t"), ("user.email", "t@example.invalid")):
            git(self.repo, "config", key, value)
        git(self.repo, "remote", "add", "origin", str(self.origin))
        self.commit("base")
        git(self.repo, "push", "-q", "origin", "main")

    def tearDown(self):
        self.tmp.cleanup()

    def commit(self, subject):
        path = self.repo / "f.txt"
        path.write_text(path.read_text() + subject + "\n" if path.exists() else subject + "\n")
        git(self.repo, "add", "f.txt")
        git(self.repo, "commit", "-q", "-m", subject)

    def branch(self, name, subjects, start="main"):
        git(self.repo, "checkout", "-q", "-b", name, start)
        for subject in subjects:
            self.commit(subject)
        git(self.repo, "push", "-q", "origin", name)
        git(self.repo, "checkout", "-q", "main")
        git(self.repo, "fetch", "-q", "origin")

    def refs(self, key):
        row = ticket_refs.scan_repo(self.repo, key)
        return row

    def cli(self, key):
        return subprocess.run([sys.executable, str(ROOT / "ticket_refs.py"), key, "--repo", str(self.repo)],
                              capture_output=True, text=True)

    def promote(self, branch, delete=False):
        git(self.repo, "merge", "-q", "--no-ff", "-m", "promote", branch)
        git(self.repo, "push", "-q", "origin", "main")
        if delete:
            git(self.repo, "push", "-q", "origin", "--delete", branch)
            git(self.repo, "branch", "-D", branch)

    def test_own_branch(self):
        self.branch("fix/PROJ-1388", ["fix: one [PROJ-1388]"])
        row = self.refs("PROJ-1388")
        self.assertEqual([b["branch"] for b in row["branches"]], ["fix/PROJ-1388"])
        self.assertTrue(row["branches"][0]["own"])
        self.assertFalse(row["dev"])
        self.assertFalse(row["prd"])
        self.assertEqual(row["prd_state"], "not-in-prd")

    def test_carried_on_other_ticket_branch_lists_co_promoted_keys(self):
        # PROJ-1389 QA fix stacked on the PROJ-1315 feature branch (2026-10-20 case)
        self.branch("fix/PROJ-1315", ["feat: select teacher [PROJ-1315]", "fix: qa [PROJ-1389]"])
        self.branch("dev", [], start="origin/fix/PROJ-1315")
        row = self.refs("PROJ-1389")
        self.assertEqual(len(row["commits"]), 1)
        self.assertEqual(row["branches"], [{"branch": "fix/PROJ-1315", "own": False, "other_keys": ["PROJ-1315"]}])
        self.assertTrue(row["dev"])
        self.assertFalse(row["stg"])

    def test_deleted_branch_remains_visible_in_prd_without_tag(self):
        self.branch("fix/PROJ-1389", ["fix: qa [PROJ-1389]"])
        self.promote("fix/PROJ-1389", delete=True)
        row = self.refs("PROJ-1389")
        self.assertTrue(row["prd"])
        self.assertEqual(row["prd_state"], "untagged")
        self.assertEqual(row["prd_tags"], [])
        self.assertEqual(row["branches"], [])
        out = self.cli("PROJ-1389")
        self.assertEqual(out.returncode, 0)
        self.assertEqual(json.loads(out.stdout)["status"], "none")

    def test_deleted_branch_remains_visible_in_prd_release_tag(self):
        self.branch("feat/PROJ-1389", ["feat: qa [PROJ-1389]"])
        self.promote("feat/PROJ-1389", delete=True)
        git(self.repo, "tag", "-a", "v261022-1", "-m", "release")
        row = self.refs("PROJ-1389")
        self.assertTrue(row["prd"])
        self.assertEqual(row["prd_state"], "tagged")
        self.assertEqual(row["prd_tags"], ["v261022-1"])
        self.assertEqual(row["branches"], [])

    def test_partial_prd_and_tag_do_not_mark_all_commits_released(self):
        self.branch("fix/PROJ-1389", ["fix: first [PROJ-1389]"])
        self.promote("fix/PROJ-1389")
        git(self.repo, "tag", "v261022-1")
        git(self.repo, "checkout", "-q", "fix/PROJ-1389")
        self.commit("fix: follow-up [PROJ-1389]")
        git(self.repo, "tag", "v261022-2")
        git(self.repo, "push", "-q", "origin", "fix/PROJ-1389")
        git(self.repo, "checkout", "-q", "main")
        row = self.refs("PROJ-1389")
        self.assertEqual(len(row["commits"]), 2)
        self.assertFalse(row["prd"])
        self.assertEqual(row["prd_state"], "not-in-prd")
        self.assertEqual(row["prd_tags"], [])
        self.assertEqual(json.loads(self.cli("PROJ-1389").stdout)["status"], "own-branch")

    def test_release_tag_must_contain_every_matching_commit(self):
        self.branch("fix/PROJ-1389", ["fix: first [PROJ-1389]"])
        git(self.repo, "tag", "v261022-1", "fix/PROJ-1389")
        # Independent commits expose checking only commits[0], regardless of log order.
        git(self.repo, "checkout", "-q", "-b", "feat/PROJ-1389")
        git(self.repo, "commit", "-q", "--allow-empty", "-m", "feat: second [PROJ-1389]")
        git(self.repo, "tag", "v261022-2")
        git(self.repo, "push", "-q", "origin", "feat/PROJ-1389")
        git(self.repo, "checkout", "-q", "main")
        self.promote("fix/PROJ-1389", delete=True)
        self.promote("feat/PROJ-1389", delete=True)
        git(self.repo, "tag", "v261022-3")
        # Branches started after release carry no unpromoted ticket commits.
        self.branch("fix/PROJ-9999", ["fix: unrelated [PROJ-9999]"])
        row = self.refs("PROJ-1389")
        self.assertEqual(len(row["commits"]), 2)
        self.assertEqual(row["prd_state"], "tagged")
        self.assertEqual(row["prd_tags"], ["v261022-3"])
        self.assertEqual(row["branches"], [])

    def test_non_prd_tag_does_not_prove_a_prd_release(self):
        self.branch("fix/PROJ-1389", ["fix: qa [PROJ-1389]"])
        self.branch("feat/PROJ-9999", ["feat: unrelated [PROJ-9999]"], start="fix/PROJ-1389")
        git(self.repo, "tag", "v261022-1", "feat/PROJ-9999")
        self.promote("fix/PROJ-1389", delete=True)
        row = self.refs("PROJ-1389")
        self.assertEqual(row["prd_state"], "untagged")
        self.assertEqual(row["prd_tags"], [])

    def test_prd_only_repo_does_not_change_another_repos_own_branch_status(self):
        self.branch("fix/PROJ-1389", ["fix: qa [PROJ-1389]"])
        self.promote("fix/PROJ-1389", delete=True)
        other = Path(self.tmp.name) / "content-api"
        subprocess.run(["git", "clone", "-q", "-b", "main", str(self.origin), str(other)], check=True)
        for key, value in (("user.name", "t"), ("user.email", "t@example.invalid")):
            git(other, "config", key, value)
        git(other, "checkout", "-q", "-b", "fix/PROJ-1389")
        git(other, "commit", "-q", "--allow-empty", "-m", "fix: follow-up [PROJ-1389]")
        git(other, "push", "-q", "origin", "fix/PROJ-1389")
        out = subprocess.run([sys.executable, str(ROOT / "ticket_refs.py"), "PROJ-1389",
                              "--repo", str(self.repo), "--repo", str(other)], capture_output=True, text=True)
        self.assertEqual(out.returncode, 0)
        self.assertEqual(json.loads(out.stdout)["status"], "own-branch")

    def test_unknown_key_and_cli_status(self):
        self.branch("fix/PROJ-1315", ["feat: x [PROJ-1315]", "fix: qa [PROJ-1389]"])
        self.assertIsNone(self.refs("PROJ-9999"))
        out = subprocess.run([sys.executable, str(ROOT / "ticket_refs.py"), "PROJ-1389", "--repo", str(self.repo)],
                             capture_output=True, text=True)
        self.assertEqual(out.returncode, 0)
        self.assertEqual(json.loads(out.stdout)["status"], "carried")
        missing = subprocess.run([sys.executable, str(ROOT / "ticket_refs.py"), "PROJ-9999", "--repo", str(self.repo)],
                                 capture_output=True, text=True)
        self.assertEqual(missing.returncode, 1)
        self.assertEqual(json.loads(missing.stdout)["status"], "none")


if __name__ == "__main__":
    unittest.main()
