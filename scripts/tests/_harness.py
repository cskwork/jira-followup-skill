"""Shared safety net and time bounds. Every test runner loads this before any test module.

- pytest: tests/conftest.py imports it.
- `python3 -m unittest discover -s tests` does not read conftest.py. discover imports modules in
  name order before running them, so test_000_harness.py (the first name) imports this module.

Two jobs, in this order:
1. Hermetic settings: drop every jira-followup setting inherited from the shell (every KEY= line in
   config.example.env plus the runtime-only names below) and point the config loader at an empty
   file. Fixtures then see the defaults (PROJ, dev/stg/main, ~/.jira-followup under each test HOME).
2. Safety values: never reach the real to-do file, the real chat CLI, or the real judge model.
   The values are forced; a test that needs its own path sets it with patch.dict inside that test.
"""
import os
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
EXAMPLE = HERE.parents[1] / "config.example.env"
HERMETIC_KEYS = set(re.findall(r"^([A-Z][A-Z0-9_]*)=", EXAMPLE.read_text(encoding="utf-8"), re.M))
HERMETIC_KEYS |= {"JIRA_FOLLOWUP_HOME", "JIRA_WAKE_DIR", "JIRA_WAKE_CWD", "JIRA_WORKER_CWD", "JIRA_API_TOKEN",
                  "FOLLOWUP_IDLE_SCAN_CMD", "FOLLOWUP_IDLE_PREFLIGHT_CMD", "JIRA_WAKE_AT", "JIRA_DREAM_AT",
                  "JIRA_WAKE_DRY_RUN", "JIRA_WAKE_FOREGROUND", "JIRA_WAKE_CHILD", "JIRA_WAKE_GUARD",
                  "JIRA_WORKER_CHECKPOINT", "JIRA_WORKER_ATTEMPT", "JIRA_HOLD_NOTIFY_FILE", "JIRA_SDLC_ROOT", "JIRA_JUDGE_SKIP",
                  "PASEO_AGENT_ID", "PASEO_WORKSPACE_ID"}
for key in HERMETIC_KEYS:
    os.environ.pop(key, None)
os.environ["JIRA_FOLLOWUP_CONFIG"] = os.devnull

# A missing executable: teams_room.call_cli ends with TEAMS_CLI_NOT_FOUND. Detached notifiers inherit it.
TEAMS_CLI = HERE / "__no_real_teams_in_tests__"
# A path under a regular file: todo.mjs refuses both reads and writes (ENOTDIR/EEXIST) instead of moving them.
TODO_FILE = Path(__file__).resolve() / "todo.md"

SAFETY = {
    "TEAMS_NATIVE_CLI": str(TEAMS_CLI),
    # Decisions and alerts go to html (a no-op here) instead of the to-do app (decision_inbox.alert_detached too).
    "JIRA_DECISION_SURFACE": "html",
    "SDLC_TODO": str(TODO_FILE),
    # The judge agent is on by default. Guard and decision tests must not start a real judge model;
    # tests/test_judge.py removes this value and tests with a mode file in a temporary state dir.
    "JIRA_JUDGE_MODE": "off",
    # Guard, preflight and jira-worker must not read a real sdlc-kit or workspace .sdlc.
    # test_lifecycle.py removes this value and points SDLC_KIT at a fake sdlc.sh.
    "JIRA_SDLC": "off",
    # The judge's staging-approval check imports check_adf and friends from the companion
    # jira-comment-format skill. Tests use the small stand-in under fixtures/ so a clean HOME works.
    "COMMENT_FORMAT_DIR": str(HERE / "fixtures/comment-format"),
}
os.environ.update(SAFETY)


def bound(seconds):
    """Scale a subprocess wait bound (hang detection, not a performance assertion) by machine load.

    One jira-worker run starts python3 about 20 times; on a heavily loaded machine a fixed 10-15 s
    bound fails. Multiplier = 1-minute load average / CPU count (1..6), or JIRA_TEST_TIMEOUT_SCALE."""
    forced = os.environ.get("JIRA_TEST_TIMEOUT_SCALE")
    if forced:
        return seconds * float(forced)
    try:
        load = os.getloadavg()[0] / (os.cpu_count() or 1)
    except OSError:
        load = 1
    return seconds * min(6.0, max(1.0, load))
