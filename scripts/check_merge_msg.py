#!/usr/bin/env python3
"""Promotion merge subject check (run right before pushing the dev/stg branch).

Usage: check_merge_msg.py --repo <worktree> --target stg [--rev HEAD]
Exit: 0 PASS, 1 FAIL, 2 usage/git error.
Why: an unattended line once pushed free-form '<KEY>·<KEY> stg 머지' subjects to several repos
because the RUNBOOK itself prescribed '<KEY> dev 머지', and non-Claude workers do not run Claude hooks.
Rule: merge(<service>): <Korean summary> -> <target branch> [PROJ-KEY]
Targets are BRANCH_DEV/BRANCH_STG/BRANCH_MAIN; the key prefix is JIRA_PROJECT (followup_config.py).
"""
import argparse, json, re, subprocess, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import followup_config as cfg  # noqa: E402


def main(argv=None):
    p = argparse.ArgumentParser()
    p.add_argument("--repo", required=True)
    p.add_argument("--target", required=True, choices=[cfg.BRANCH_DEV, cfg.BRANCH_STG, cfg.BRANCH_MAIN])
    p.add_argument("--rev", default="HEAD")
    a = p.parse_args(argv)
    r = subprocess.run(["git", "-C", a.repo, "log", "-1", "--format=%s%n%P", a.rev], capture_output=True, text=True)
    if r.returncode:
        print(json.dumps({"check": "merge-msg", "status": "ERROR"}))
        return 2
    subject, parents = (r.stdout.splitlines() + ["", ""])[:2]
    pattern = rf"^merge\([a-z0-9-]+\): .*[가-힣].* -> {re.escape(a.target)} (\[" + re.escape(cfg.PROJECT) + r"-\d+\])+$"
    ok = len(parents.split()) == 2 and re.match(pattern, subject) is not None
    print(json.dumps({"check": "merge-msg", "status": "PASS" if ok else "FAIL", "subject": subject,
                      "merge_commit": len(parents.split()) == 2, "expected": pattern}, ensure_ascii=False))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
