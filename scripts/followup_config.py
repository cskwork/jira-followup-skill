"""Shared settings for every jira-followup script (no third-party dependencies).

Values come from the environment first, then from `$JIRA_FOLLOWUP_HOME/config.env`
(default `~/.jira-followup/config.env`, git-ignored, never committed). Copy
`config.example.env` from the skill root to start. Nothing here holds a secret:
credentials stay in `acli`, `jk`, your DB tool, or files the config points to.
"""
import os
import re
from pathlib import Path
from zoneinfo import ZoneInfo

SKILL_DIR = Path(__file__).resolve().parent.parent
HOME_DIR = Path(os.path.expanduser(os.environ.get("JIRA_FOLLOWUP_HOME", "~/.jira-followup")))


def load_config(path):
    """Fill os.environ from KEY=VALUE lines without overriding values already set."""
    path = Path(path)
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            value = value.strip().strip('"').strip("'")
            if value:  # empty means "use the default", never an empty override
                os.environ.setdefault(key.strip(), os.path.expanduser(value))


load_config(os.environ.get("JIRA_FOLLOWUP_CONFIG", HOME_DIR / "config.env"))


def get(name, default=""):
    return os.environ.get(name) or default


def get_list(name, default):
    """Comma-separated list setting."""
    return tuple(part.strip() for part in get(name, default).split(",") if part.strip())


# Jira
PROJECT = get("JIRA_PROJECT", "PROJ")
ME = get("JIRA_ACCOUNT_ID", "unset-account-id")
JIRA_SITE = get("JIRA_SITE", "jira.example.com")
BROWSE = f"https://{JIRA_SITE}/browse/"
TICKET_RE = re.compile(re.escape(PROJECT) + r"-\d+")
TZ = ZoneInfo(get("JIRA_FOLLOWUP_TZ", "Asia/Seoul"))

# Workflow statuses (Jira display names) in promotion order; see config.example.env.
STATUS_ORDER = list(get_list("STATUS_ORDER", "진행 중,개발 QA 요청,개발 검수 완료,스테이징 QA 요청,"
                                             "스테이징 검수 완료,릴리즈 대기,운영계 배포 완료"))
DONE_NOT_RELEASED = get_list("STATUS_DONE_NOT_RELEASED", "개발 검수 완료,스테이징 QA 요청,스테이징 검수 완료,릴리즈 대기")
POST_RELEASE_STATUSES = get_list("STATUS_POST_RELEASE", "운영계 배포 완료,운영 검수 완료")
TODO_STATUS = get("STATUS_TODO", "할 일")
SPINOFF_ISSUE_TYPE = get("SPINOFF_ISSUE_TYPE", "버그")

# Workspace and git
WORKSPACE = Path(os.path.expanduser(get("JIRA_FOLLOWUP_WORKSPACE", str(Path.cwd()))))
REPO_GLOB = get("REPO_GLOB", "*")
BRANCH_DEV = get("BRANCH_DEV", "dev")
BRANCH_STG = get("BRANCH_STG", "stg")
BRANCH_MAIN = get("BRANCH_MAIN", "main")

# Databases (dev and staging only; there is never a production setting)
DB_QUERY_CMD = get("DB_QUERY_CMD", "")
# Profiles are "dev" and "stg" (staging). DB_QUERY_CMD runs `<cmd> <config-name> <sql>` and prints
# JSON {"rows": [...]}; config names are <db><suffix>, e.g. app_dev / app_stg.
DB_CONFIG_SUFFIX = {"dev": get("DB_CONFIG_SUFFIX_DEV", "_dev"), "stg": get("DB_CONFIG_SUFFIX_STG", "_stg")}
# `SELECT @@hostname` must start with this prefix on the same connection (dev and staging may share a VIP).
DB_HOST_PREFIX = {"dev": get("DB_HOST_PREFIX_DEV", "dev-db"), "stg": get("DB_HOST_PREFIX_STG", "stg-db")}
DB_PRIMARY = get("DB_PRIMARY", "app")

# Companion skills and tools
COMMENT_FORMAT_DIR = Path(os.path.expanduser(get("COMMENT_FORMAT_DIR", "~/.agents/skills/jira-comment-format/scripts")))
SDLC_KIT = Path(os.path.expanduser(get("SDLC_KIT", "~/.agents/skills/sdlc-kit")))
JIRA_REAUTH_HINT = get("JIRA_REAUTH_HINT", "acli jira auth login")
LINE_TITLE = get("LINE_TITLE", "Jira follow-up line")

# --- Worker runtime, resource locks, factory workspace ---
# Service repository directory = WORKSPACE/<SERVICE_REPO_PREFIX><service>; `deploy-<service>` resource locks need it.
SERVICE_REPO_PREFIX = get("SERVICE_REPO_PREFIX", "")
# Per-ticket orchestrator agent started by jira-ticket-run.
TICKET_ORCHESTRATOR_PROVIDER = get("TICKET_ORCHESTRATOR_PROVIDER", "claude")
TICKET_ORCHESTRATOR_MODEL = get("TICKET_ORCHESTRATOR_MODEL", "claude-opus-5-5")
TICKET_ORCHESTRATOR_THINKING = get("TICKET_ORCHESTRATOR_THINKING", "high")
TICKET_ORCHESTRATOR_MODE = get("TICKET_ORCHESTRATOR_MODE", "bypassPermissions")

# --- Decisions, chat room, judge ---
# Teams room watcher (teams_room.py). Empty room = watcher off (scan reports configured=false).
TEAMS_ROOM_CHAT_ID = get("TEAMS_ROOM_CHAT_ID", "")
TEAMS_ROOM_NAME = get("TEAMS_ROOM_NAME", "Teams room")
TEAMS_ME_MRI = get("TEAMS_ME_MRI", "")
TEAMS_ME_NAME = get("TEAMS_ME_NAME", "")
TEAMS_ME_NAMES = get_list("TEAMS_ME_NAMES", "")
TEAMS_SELF_CHAT_ID = get("TEAMS_SELF_CHAT_ID", "48:notes")
# Jira status a reporter moves a ticket to after checking dev (judge.py stg rule)
STATUS_DEV_DONE = get("STATUS_DEV_DONE", "개발 검수 완료")
# Judge agent (judge.py): a claude-CLI-compatible command, model and effort
JUDGE_PROVIDER = get("JUDGE_PROVIDER", "claude")
JUDGE_MODEL = get("JUDGE_MODEL", "claude-opus-5-5")
JUDGE_THINKING = get("JUDGE_THINKING", "medium")

# --- Morning check, kube, sdlc dreamer ---
# Morning check moves a ticket automatically only from this status (or later) to STATUS_ON_PROD;
# empty = the fifth STATUS_ORDER entry.
STATUS_QA_DONE = get("STATUS_QA_DONE") or STATUS_ORDER[min(4, len(STATUS_ORDER) - 1)]
# Repository whose origin/retro/* proposal branches the morning check lists (default: this skill repo).
RETRO_REPO = Path(os.path.expanduser(get("RETRO_REPO", str(SKILL_DIR))))
# Recovery hint shown when the chat-room watcher has been failing for over 24 hours.
CHAT_RECONNECT_HINT = get("CHAT_RECONNECT_HINT", "<채팅 CLI 스킬> connect")
# kube_pods.py: `<KUBE_CONNECT_CMD> <host> "<kubectl ...>"`; empty = kube checks not configured (SKIP).
KUBE_CONNECT_CMD = get("KUBE_CONNECT_CMD", "")
KUBE_HOST_DEV = get("KUBE_HOST_DEV", "dev")
KUBE_HOST_STG = get("KUBE_HOST_STG", "stg")
# dream_tick.py: the sdlc memory dreamer agent.
DREAM_PROVIDER = get("DREAM_PROVIDER", "claude")
DREAM_MODEL = get("DREAM_MODEL", "claude-opus-5-5")
DREAM_THINKING = get("DREAM_THINKING", "medium")
DREAM_MODE = get("DREAM_MODE", "bypassPermissions")
