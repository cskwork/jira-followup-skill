#!/usr/bin/env python3
"""Find where a ticket's commits live across the workspace service repos (read-only).

Usage: ticket_refs.py PROJ-123 [--root WORKSPACE] [--repo PATH ...]
Exit: 0 found, 1 no commit with [KEY] in the searched refs, 2 usage/git error.

Why: RUNBOOK §2 used to look only for origin/fix/<KEY>. A ticket whose commits rode on another
ticket's branch (origin/fix/<OTHER>) in several repos, already dev-merged, was judged "브랜치 없음".
Commits are searched in origin/fix/*, origin/feat/* and origin/<main> by "[KEY]".
Branch names come from followup_config.py: BRANCH_DEV, BRANCH_STG, BRANCH_MAIN ("prd" below).
Branches carrying commits beyond prd are reported, with other keys that promotion moves too.
The existing status describes those unpromoted branches; prd-only matches have status "none"
but exit 0. prd/prd_state describe all matched commits, independently of branch availability.

Output (one JSON object):
{"key", "status": "own-branch" | "carried" | "none",
 "repos": [{"repo", "commits": [sha], "branches": [{"branch", "own", "other_keys": [..]}],
            "dev": bool, "stg": bool, "prd": bool,
            "prd_state": "tagged" | "untagged" | "not-in-prd", "prd_tags": [..]}]}

prd_tags contains only v* tags on prd history containing every matched commit.
These are Git ancestry facts from local refs, not proof of a running deployment.
"""

import argparse
import json
from pathlib import Path
import re
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import followup_config as cfg  # noqa: E402

DEV, STG, MAIN = ("origin/" + b for b in (cfg.BRANCH_DEV, cfg.BRANCH_STG, cfg.BRANCH_MAIN))
KEY_RE = re.compile(r"\[([A-Z][A-Z0-9]+-\d+)\]")


def git(repo, *args):
    p = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=120)
    if p.returncode:
        raise RuntimeError(f"git {' '.join(args[:2])} failed in {Path(repo).name}")
    return p.stdout.strip()


def has_ref(repo, ref):
    return subprocess.run(["git", "-C", str(repo), "rev-parse", "--verify", "-q", ref],
                          capture_output=True, text=True).returncode == 0


def is_ancestor(repo, commit, ref):
    if not has_ref(repo, ref):
        return False
    return subprocess.run(["git", "-C", str(repo), "merge-base", "--is-ancestor", commit, ref],
                          capture_output=True).returncode == 0


def scan_repo(repo, key):
    if not has_ref(repo, MAIN):
        return None
    marker = f"[{key}]"
    out = git(repo, "log", "--format=%H", "-F", f"--grep={marker}",
              "--remotes=origin/fix/*", "--remotes=origin/feat/*", MAIN)
    commits = [c for c in out.splitlines() if c]
    if not commits:
        return None
    pending = [c for c in commits if not is_ancestor(repo, c, MAIN)]
    branches = set()
    for commit in pending:
        listed = git(repo, "branch", "-r", "--contains", commit, "--list", "origin/fix/*", "origin/feat/*")
        branches.update(b.strip() for b in listed.splitlines() if b.strip() and "->" not in b)
    rows = []
    for branch in sorted(branches):
        subjects = git(repo, "log", "--format=%s", f"{MAIN}..{branch}")
        others = sorted({k for s in subjects.splitlines() for k in KEY_RE.findall(s)} - {key})
        rows.append({"branch": branch.removeprefix("origin/"),
                     "own": branch in (f"origin/fix/{key}", f"origin/feat/{key}"), "other_keys": others})
    tags = set()
    if not pending:
        tags = set(git(repo, "tag", "--contains", commits[0], "-l", "v*").splitlines())
        for commit in commits[1:]:
            if not tags:
                break
            tags.intersection_update(git(repo, "tag", "--contains", commit, "-l", "v*").splitlines())
        tags = {tag for tag in tags if is_ancestor(repo, f"refs/tags/{tag}", MAIN)}
    return {"repo": Path(repo).name, "commits": [c[:9] for c in commits], "branches": rows,
            "dev": all(is_ancestor(repo, c, DEV) for c in commits),
            "stg": all(is_ancestor(repo, c, STG) for c in commits),
            "prd": not pending,
            "prd_state": "not-in-prd" if pending else "tagged" if tags else "untagged",
            "prd_tags": sorted(tags)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("key")
    parser.add_argument("--root", default=str(cfg.WORKSPACE))
    parser.add_argument("--repo", action="append", default=[], help="repo path (repeatable); default: <root>/<REPO_GLOB>")
    args = parser.parse_args(argv)
    if not re.fullmatch(r"[A-Z][A-Z0-9]+-\d+", args.key):
        parser.error("key must look like PROJ-123")
    repos = [Path(r) for r in args.repo] or sorted(p for p in Path(args.root).glob(cfg.REPO_GLOB) if (p / ".git").exists())
    try:
        found = [row for row in (scan_repo(r, args.key) for r in repos) if row]
    except (RuntimeError, subprocess.SubprocessError, OSError) as error:
        print(json.dumps({"key": args.key, "error": str(error)}, ensure_ascii=False))
        return 2
    branch_rows = [row for row in found if row["branches"]]
    if not branch_rows:
        status = "none"
    elif all(any(b["own"] for b in row["branches"]) for row in branch_rows):
        status = "own-branch"
    else:
        status = "carried"
    print(json.dumps({"key": args.key, "status": status, "repos": found}, ensure_ascii=False))
    return 0 if found else 1


if __name__ == "__main__":
    sys.exit(main())
