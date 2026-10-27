#!/usr/bin/env python3
"""Put one Jira morning check into the developer-note board as ONE work item (optional).

Reads $JIRA_FOLLOWUP_HOME/YYYY-MM-DD.md (RUNBOOK §4), strips every Jira link so ticket keys
stay plain text, and creates or updates the item "아침 점검 YYYY-MM-DD"
(label daily-report-YYYY-MM-DD, status Done). Re-running the same day updates that item.

Settings (config.env or environment), the same CLI contract as the companion skill
jira-comment-format (scripts/dev_note.py):
  DEV_NOTE_CLI        board CLI path (.py runs with python3); JSON on stdout
  DEV_NOTE_BASE       board server URL, passed as --base
  DEV_NOTE_SPACE      project/space key for the items
  DEV_NOTE_WORKSPACE  (optional) stop unless `whoami` organization.id matches
  DEV_NOTE_LOGIN_HINT (optional) command shown when the session expired (401)
Final stdout line: `devnote=ok item=<ITEM> created|updated|unchanged` or `devnote=BLOCKED <reason>` (exit 3).
"""
import argparse
import datetime
import json
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import followup_config as cfg  # noqa: E402


class Blocked(Exception):
    pass


def cli():
    path = cfg.get("DEV_NOTE_CLI")
    if not path or not cfg.get("DEV_NOTE_BASE") or not cfg.get("DEV_NOTE_SPACE"):
        raise Blocked("DEV_NOTE_CLI·DEV_NOTE_BASE·DEV_NOTE_SPACE 미설정")
    path = os.path.expanduser(path)
    return ([sys.executable, path] if path.endswith(".py") else [path]) + ["--base", cfg.get("DEV_NOTE_BASE")]


def note(*args, stdin=None):
    p = subprocess.run(cli() + list(args), input=stdin, capture_output=True, text=True, timeout=120)
    if p.returncode != 0:
        err = ((p.stderr or p.stdout).strip().splitlines() or ["unknown"])[-1]
        if "401" in err:
            raise Blocked("노트 보드 세션 만료(401): %s" % cfg.get("DEV_NOTE_LOGIN_HINT", "보드 CLI로 다시 로그인"))
        raise RuntimeError("devnote %s failed: %s" % (" ".join(args[:2]), err))
    out = p.stdout.strip()
    return json.loads(out) if out[:1] in "[{" else out


def strip_jira_links(text, site=None):
    site = re.escape(site or cfg.JIRA_SITE)
    # [PROJ-1](https://<site>/browse/PROJ-1) -> PROJ-1
    text = re.sub(r"\[([^\]]+)\]\(https?://[^)\s]*" + site + r"[^)\s]*\)", r"\1", text)
    # bare Jira URLs
    return re.sub(r"<?https?://[^\s)>]*" + site + r"[^\s)>]*>?", "", text)


def main(date, directory):
    report = Path(directory) / ("%s.md" % date)
    body = strip_jira_links(report.read_text())[:100000]
    workspace = cfg.get("DEV_NOTE_WORKSPACE")
    if workspace and note("whoami").get("organization", {}).get("id") != workspace:
        raise Blocked("노트 보드 워크스페이스 불일치")
    space = cfg.get("DEV_NOTE_SPACE")
    label = "daily-report-%s" % date
    summary = "아침 점검 %s" % date
    found = note("search", "--jql", 'project = %s AND labels = "%s"' % (space, label), "--limit", "5")
    items = found.get("items", []) if isinstance(found, dict) else []
    if items:
        key = items[0]["key"]
        cur = note("issue", "get", key)
        if cur.get("description") == body and cur.get("summary") == summary:
            result = "unchanged"
        else:
            note("issue", "update", key, "--json", "-",
                 stdin=json.dumps({"summary": summary, "description": body}, ensure_ascii=False))
            result = "updated"
    else:
        key = note("issue", "create", "--project", space, "--json", "-",
                   stdin=json.dumps({"summary": summary, "description": body,
                                     "labels": [label, "daily-report"]}, ensure_ascii=False))["key"]
        result = "created"
    item = note("issue", "get", key)
    if item.get("status") != "Done":
        note("issue", "transition", key, "--status", "Done")
        item = note("issue", "get", key)
    if item.get("description") != body or item.get("status") != "Done":
        raise RuntimeError("%s 되읽기 불일치" % key)
    print("devnote=ok item=%s %s" % (key, result))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("date", nargs="?", default=datetime.date.today().isoformat())
    parser.add_argument("--dir", type=Path, default=Path(os.environ.get("JIRA_WAKE_DIR") or cfg.HOME_DIR))
    args = parser.parse_args()
    try:
        main(args.date, args.dir)
    except Blocked as e:
        print("devnote=BLOCKED %s" % e)
        sys.exit(3)
