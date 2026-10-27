# jira-followup

An agent skill that runs a scheduled Jira follow-up loop for one developer (the operator): a cheap guard decides whether there is work, a minimal orchestrator directs, workers reproduce, fix and deploy to dev, deterministic scripts decide "done", and a human approves anything that reaches staging.

> **Cheapest judge first. The orchestrator only directs. Scripts decide done. Humans decide by button.**

The skill documents (`SKILL.md`, `RUNBOOK.md`, `ORCHESTRATOR.md`, `RETRO.md`) are written in Korean for their original audience. Scripts, flags and this README are in English.

## What it does

Every 30 minutes on working days (your schedule), one tick runs:

1. **Guard and waker** (`jira-daily-wake` → `jira-daily-guard`, no model). It takes the run lock and checks for duplicates. It then scans Jira and the local queue. When nothing is actionable it exits before any expensive model starts.
2. **Morning check** (first tick of the day, `morning_check.py`). It compares the operator's own in-flight tickets with where their commits actually are (dev / stg / main branches and release tags). It plans status moves in your workflow order, applies them with `--execute` and reads them back, then writes a daily report. The report can optionally go to a developer-note board.
3. **Follow-up** (every tick, RUNBOOK §7). It picks up comments that mention the operator, new comments on the operator's tickets, and the operator's own backlog tickets:
   - **Intent gate.** sdlc-kit intent phase. Work starts only at **≥ 90 %** clarity.
   - **Below 90 %,** it reproduces the *observable* gaps first (data, screen, logs). It asks the requester only about *judgement* gaps such as expected behaviour or priority.
   - **Fix.** Local AS-IS repro → fix on `fix/<KEY>` → local TO-BE → dev deploy and smoke.
   - **Evidence.** A planner-readable Jira comment with before/after screenshots, written through the companion skill [`jira-comment-format`](https://github.com/cskwork/jira-comment-format-skill). Read-only check queries are attached as `.sql` file cards. Developer detail goes to the optional dev-note board.
   - **Staging deploy** waits for a human approval click on a local decision page.
4. **Verification and memory.** Completion is judged by verifier JSON from `check_base.py`, `check_merge_msg.py`, `check_deploy.py`, `check_migrations.py`, `ticket_refs.py` and comment read-back. Mistakes go to `lessons.jsonl`. A weekly retro (`RETRO.md`) proposes rule changes on a branch for a human to merge.

Safety rails:
- A run lock with ownership tokens.
- A sequential drain of the queue (one unit at a time).
- Per-item backoff.
- No production writes: no main-branch push, no tags, no production jobs, no production DB.
- No force push.
- Existing Jira comments are never edited.

## Architecture

```
scheduler tick ──> jira-daily-wake ──> jira-daily-guard (lock · dedupe · followup_idle.py)
                                          │ idle → stop (no expensive model)
                                          ▼ work
                                   orchestrator (ORCHESTRATOR.md, high reasoning, directs only)
                                      │ jira-worker sol|opus  (brief + cited RUNBOOK sections)
                                      ▼
                         workers: repro · fix · dev deploy · Jira evidence
                                      │
                       verifiers (exit code + JSON) ──> queue/state via state_update.py
                                      │ needs a human
                                      ▼
                     decision-request → decision_page.py (127.0.0.1, one-time token, one click)
                                      └─> decisions.jsonl → jira-daily-wake --now --key <KEY>
```

See `docs/architecture.html` for the full picture (Korean).

| Lane | Who | Does | Never does |
|---|---|---|---|
| script | guard, idle, triage rules, verifiers | lock/dedupe/idle gate, deterministic judgements | judge prose |
| cheap model | waker, shadow classifier (`TRIAGE_MODEL`) | wake the line, classify one comment at a time (logged only until `enforce`) | change state |
| orchestrator | `ORCHESTRATOR_MODEL`, high reasoning | pick units, write briefs, check verifier JSON, report, release the lock | edit code, push, deploy |
| workers | `sol` (codex) / `opus` (claude), configurable | repro, fix, dev deploy, Jira evidence | widen the brief, touch the lock |
| human | the operator | VPN/OTP, staging approval, judgement answers | routine relaying |

## Relation to dark-factory

[cskwork/dark-factory](https://github.com/cskwork/dark-factory) is the **portable Python harness** version of the same loop: factory-wake / factory-guard / orchestrator / verifiers / decision pages, independent of any tracker or agent product. This repository is the **agent-skill form**: a Jira + git + Jenkins + Paseo flavoured runbook that an agent follows directly, with the scripts it calls. Use dark-factory to build your own line; use this skill if your stack already looks like this one.

## Requirements

- Python 3.11+ (standard library only), bash, git
- [`acli`](https://developer.atlassian.com/cloud/acli/) logged in to your Jira site (`acli jira auth login`)
- `jk` (Jenkins CLI) contexts for dev and stg (deploy verification)
- Paseo (or any scheduler able to run `jira-daily-wake`, plus an agent runner with the same `run` flags)
- Companion skills: [`jira-comment-format`](https://github.com/cskwork/jira-comment-format-skill) (Jira writing, v0.2.0+ for `.sql` file cards), [`sdlc-kit`](https://github.com/cskwork/sdlc-kit) (intent → plan → build → evidence); optionally [`browser-qa`](https://github.com/cskwork/browser-qa) for screen evidence
- Your own equivalents of the roles the RUNBOOK names in angle brackets: `<배포 스킬>` (deploy), `<로컬 기동 스킬>` (local run), `<시험 계정 스킬>` (test accounts), `<DB 조회 스킬>` (read-only DB), `<기획 정본 저장소>` (planning source of truth)

## Setup

```bash
git clone <this repo> ~/.agents/skills/jira-followup
mkdir -p ~/.jira-followup
cp ~/.agents/skills/jira-followup/config.example.env ~/.jira-followup/config.env
$EDITOR ~/.jira-followup/config.env        # site, project, accountId, statuses, branches, workspace

# entry points on PATH
for f in jira-daily-wake jira-daily-guard jira-worker jira-followup-approve decision-request stg-approval-request; do
  ln -sfn ~/.agents/skills/jira-followup/scripts/$f ~/.local/bin/$f
done
```

Schedule one tick every 30 minutes on working days, for example cron `0,30 9-18 * * 1-5`. Run it in the workspace directory with the prompt *"Run `jira-daily-wake` only (timeout ≥ 600000 ms) and answer with its last line."* Use a cheap model for that prompt. Manual run: `jira-daily-wake --now --key PROJ-123`. Staging approval from the CLI: `jira-followup-approve PROJ-123 stg "<approval text>"`.

## Configuration

All settings live in `$JIRA_FOLLOWUP_HOME/config.env` (default `~/.jira-followup/config.env`, outside the repo). Environment variables override it. Python scripts read it through `scripts/followup_config.py` and shell entry points load it themselves. `config.example.env` documents every key. The main groups:

| Group | Keys |
|---|---|
| Jira | `JIRA_SITE`, `JIRA_PROJECT`, `JIRA_ACCOUNT_ID`, `JIRA_EMAIL`, `JIRA_TOKEN_FILE` / `JIRA_API_TOKEN`, `JIRA_REAUTH_CMD` |
| Workflow | `STATUS_ORDER`, `STATUS_ON_DEV/STG/PROD`, `STATUS_DONE_NOT_RELEASED`, `STATUS_POST_RELEASE`, `STATUS_TODO`, `SPINOFF_ISSUE_TYPE` |
| Git | `JIRA_FOLLOWUP_WORKSPACE`, `REPO_GLOB`, `BRANCH_DEV`, `BRANCH_STG`, `BRANCH_MAIN`, `MIGRATION_GLOB` |
| Environments | `VPN_STATUS_CMD`, `DB_QUERY_CMD`, `DB_TOOL_DIR`, `DB_PRIMARY`, `DB_CONFIG_SUFFIX_*`, `DB_HOST_PREFIX_*`, `DB_SERVICE_MAP`, `*_HEALTH_URL_*`, `JK_CONTEXT_DEV` |
| Agents | `PASEO_BIN`, `PASEO_ONCE_BIN`, `ORCHESTRATOR_*`, `WORKER_SOL_*`, `WORKER_OPUS_*`, `TRIAGE_MODEL` |
| Optional | `DEV_NOTE_*` (dev-note board), `FOLLOWUP_IDLE_TEAMS_CMD` / `CHAT_SEND_CMD` (chat watcher and approved replies), `COMMENT_FORMAT_DIR`, `SDLC_KIT` |

Credentials never live in the repo. Use `acli` and `jk` logins, a token file path, or exported environment variables. State files are kept under `$JIRA_FOLLOWUP_HOME`, outside git: `state.json`, `followup-queue.json`, `approvals.json`, journals, `decisions.jsonl` and `lessons.jsonl`.

## Tests

```bash
cd scripts && python3 -m unittest discover -s tests
```

The tests run offline. Jira, git, Jenkins, DB and HTTP are injected or faked. `tests/test_00_hermetic.py` runs first under `discover`; it clears any inherited jira-followup settings and points the loader at an empty config, so a personal `config.env` cannot change the fixtures. When you run a single test module directly, unset those variables yourself.

## Not included

- **Chat-room watcher.** The original read one specific Teams room through a private tool. Plug in any watcher through `FOLLOWUP_IDLE_TEAMS_CMD`; it must write `{"status":"ok","events":[...]}`. Send approved drafts through `CHAT_SEND_CMD`.
- **Error-signature log probe test.** It depended on a private log-query skill.
- **Architecture PNG renderings.** They contained internal identifiers; `docs/architecture.html` is the sanitized version.

## License

MIT, see [LICENSE](LICENSE).
