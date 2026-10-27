#!/usr/bin/env python3
"""Check that a ticket branch contains only its own non-merge commits.

Usage: check_base.py --repo DIR --branch REF --key PROJ-123
                     [--also-key KEY ...] [--base origin/main] [--feature-side]

Each --also-key adds one allowed [KEY] subject marker; repeat the option as needed.
No refs are fetched or changed. By default the local base..branch range is checked.
With --feature-side, a tip with exactly two parents checks parent 2 instead; merges
inside that history still fail. checked_commit identifies the revision checked.
The number of base commits absent from that revision (behind) is informational only.
Stdout contains one JSON object, except for --help. Exit codes: 0 PASS, 1 FAIL
(merge commit or missing ticket marker), 2 invalid usage or a Git error.
"""

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import followup_config as cfg  # noqa: E402

TIMEOUT_SECONDS = 30
BASE = "origin/" + cfg.BRANCH_MAIN


class CheckError(Exception):
    """An error whose message is safe to include in JSON output."""


class Parser(argparse.ArgumentParser):
    def error(self, message):
        # argparse messages may contain arbitrary supplied argument values.
        raise CheckError("invalid command line arguments; see --help")


def git(repo, stage, *args):
    """Run read-only Git commands without exposing Git's stderr or environment."""
    try:
        result = subprocess.run(
            ["git", "--no-pager", "-C", repo, *args],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise CheckError("git %s timed out after %d seconds" % (stage, TIMEOUT_SECONDS)) from None
    except OSError:
        raise CheckError("git %s could not be executed" % stage) from None
    if result.returncode:
        raise CheckError("git %s failed (exit %d)" % (stage, result.returncode))
    return result.stdout


def resolve(repo, ref, label):
    sha = git(repo, "resolve " + label, "rev-parse", "--verify", "--end-of-options", ref + "^{commit}").strip()
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", sha):
        raise CheckError("git resolve %s returned an invalid commit SHA" % label)
    return sha


def check(args, output):
    for key in [args.key, *args.also_key]:
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*-[0-9]+", key):
            raise CheckError("invalid ticket key; expected a value such as PROJ-123")

    # Only resolved commit SHAs enter range/revision commands below.
    branch_head = resolve(args.repo, args.branch, "branch")
    if args.feature_side:
        parents = git(args.repo, "tip parents", "rev-list", "--parents", "--max-count=1", branch_head, "--").split()[1:]
        if len(parents) == 2:
            branch_head = parents[1]
    output["checked_commit"] = branch_head
    base_head = resolve(args.repo, args.base, "base")
    output["base_head"] = base_head
    output["merge_base"] = git(args.repo, "merge-base", "merge-base", base_head, branch_head).strip()
    behind = git(args.repo, "behind count", "rev-list", "--count", branch_head + ".." + base_head, "--").strip()
    if not behind.isdigit():
        raise CheckError("git behind count returned an invalid count")
    output["behind"] = int(behind)

    history = git(args.repo, "commit log", "log", "--no-show-signature", "-z", "--format=%H%x00%P%x00%s", base_head + ".." + branch_head, "--")
    fields = history.split("\0") if history else []
    if fields and fields[-1] == "":
        fields.pop()
    if len(fields) % 3:
        raise CheckError("git commit log returned an invalid record")
    markers = ["[" + key + "]" for key in [args.key, *args.also_key]]
    for offset in range(0, len(fields), 3):
        sha, parents, subject = fields[offset:offset + 3]
        reasons = []
        if len(parents.split()) > 1:
            reasons.append("merge commit")
        if not any(marker in subject for marker in markers):
            reasons.append("subject missing an allowed ticket marker")
        output["commits"].append({
            "sha": sha,
            "subject": subject,
            "ok": not reasons,
            "reason": "; ".join(reasons),
        })
    output["status"] = "PASS" if all(commit["ok"] for commit in output["commits"]) else "FAIL"
    return 0 if output["status"] == "PASS" else 1


def main(argv=None):
    parser = Parser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter, allow_abbrev=False)
    parser.add_argument("--repo", required=True, help="local Git repository directory")
    parser.add_argument("--branch", required=True, help="ticket branch ref or commit")
    parser.add_argument("--key", required=True, help="required ticket key, for example PROJ-123")
    parser.add_argument("--also-key", action="append", default=[], help="additional allowed ticket key (repeatable)")
    parser.add_argument("--base", default=BASE, help="local base ref (default: %s)" % BASE)
    parser.add_argument("--feature-side", action="store_true", help="check parent 2 of an exactly two-parent tip")
    output = {"check": "base", "status": "FAIL", "branch": None, "key": None,
              "commits": [], "merge_base": None, "base_head": None, "behind": None,
              "checked_commit": None}
    try:
        args = parser.parse_args(argv)
        output.update(branch=args.branch, key=args.key)
        exit_code = check(args, output)
    except CheckError as error:
        output["error"] = str(error)
        exit_code = 2
    print(json.dumps(output, ensure_ascii=False))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
