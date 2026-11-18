# jira-followup

An agent skill that runs a scheduled Jira follow-up loop for one developer (the operator): a cheap guard decides whether there is work, a top orchestrator hands tickets out, one ticket orchestrator per ticket drives it through reproduce → fix → dev deploy → evidence, deterministic scripts decide "done", a judge agent settles low-risk decisions, and a human approves anything risky or anything that reaches staging.

> **Cheapest judge first. Orchestrators only direct. Scripts decide done. Humans decide by button or reply.**

The skill documents (`SKILL.md`, `RUNBOOK.md`, `ORCHESTRATOR.md`, `ORCHESTRATOR-REFERENCE.md`, `TICKET-ORCHESTRATOR.md`, `RETRO.md`, `docs/*.md`) are written in Korean for their original audience. Scripts, flags and this README are in English.

## What it does

Every 30 minutes on working days (your schedule), one tick runs:

1. **Guard and waker** (`jira-daily-wake` → `jira-daily-guard`, no model). It takes the run lock and checks for duplicates. It then scans Jira, the local queue, the optional chat room and the to-do app. When nothing is actionable it exits before any expensive model starts. An idle tick may start the daily memory cleanup (see *Dream* below).
2. **Morning check** (first tick of the day, `morning_check.py`). It compares the operator's own in-flight tickets with where their commits actually are (dev / stg / main branches and release tags). It writes a daily report with a dependency health table (Jira auth, git fetch, guard runs, VPN, cluster access, chat watcher) so a broken dependency never reads as "0 errors". Automatic status moves are limited to release steps after QA (QA-stage moves need posted evidence, see step 3). An action that carries over three reports in a row, or an unmerged retro proposal older than 7 days, becomes one decision.
3. **Follow-up** (every tick, RUNBOOK §7). It picks up comments that mention the operator, new comments on the operator's tickets, the operator's own backlog tickets, and replies in the to-do app:
   - **Intent gate.** sdlc-kit intent phase. Work starts only at **≥ 90 %** clarity.
   - **Below 90 %,** it reproduces the *observable* gaps first (data, screen, logs). It asks the requester only about *judgement* gaps such as expected behaviour or priority.
   - **Fix.** Local AS-IS repro → fix on `fix/<KEY>` → local TO-BE → `sdlc.sh check ship` gate → dev deploy and smoke.
   - **Evidence.** A planner-readable Jira comment with before/after screenshots, written through the companion skill [`jira-comment-format`](https://github.com/cskwork/jira-comment-format-skill). Read-only check queries are attached as `.sql` file cards and developer detail as one `.txt` attachment on the same comment. The Jira status moves only after that comment is posted and read back (`jira_status_check.py --evidence-comment`).
   - **Staging deploy** waits for an approval: a human click or reply, or the judge when the reporter/QA has already confirmed dev.
4. **Verification and memory.** Completion is judged by caller-declared requirements and verifier JSON from `check_base.py`, `check_merge_msg.py`, `check_deploy.py`, `check_migrations.py`, `ticket_refs.py`, `lifecycle.py check-ship` and comment read-back. Failures go to `lessons.jsonl` under a closed set of classes; a lesson that repeats 3 times in 14 days opens a `fix-now` / `accept` / `ignore` decision, and the last 14 days are summarised into orchestrator and worker prompts. A weekly retro (`RETRO.md`) proposes rule changes on a branch for a human to merge.

Safety rails:
- A global run lock and per-ticket leases, both with ownership tokens; a ticket token can only write its own key.
- Resource locks (`runtime-local`, `deploy-<service>`) so parallel tickets take turns on the local server and on each deploy pipeline.
- Per-item backoff, lost-run and lost-lease recovery.
- A factory hold (`jira-factory-hold`) that narrows or freezes the line until a set time.
- No production writes: no main-branch push, no tags, no production jobs, no production DB.
- No force push. Existing Jira comments are never edited.

## Architecture

```
scheduler tick ──> jira-daily-wake ──> jira-daily-guard (lock · dedupe · hold · followup_idle.py)
                         │                │ idle → stop (no expensive model) ──> dream_tick.py (once a day)
                         │                ▼ work
                         │       top orchestrator (ORCHESTRATOR.md, high reasoning, hands out keys)
                         │                │ run_lock.py lease-acquire → jira-ticket-run   (up to `lanes`)
                         │                ▼
                         │       ticket orchestrator per key (TICKET-ORCHESTRATOR.md, ticket_packet.py first)
                         │                │ jira-worker sol|opus --requirements --resources (cited RUNBOOK sections)
                         │                ▼
                         │       workers: repro · fix · dev deploy · Jira evidence
                         │                │
                         │   verifiers (exit code + JSON) ──> drain.py check-result ──> state_update.py
                         │                │ needs a human
                         │                ▼
                         │   decision-request ──> judge.py (low risk?) ──> decided by=judge
                         │                │ otherwise
                         │                ▼
                         │   decision surface: to-do app item (reply) | decision_page.py (127.0.0.1, one click)
                         └── decision_replies.py ──> decisions.jsonl ──> jira-daily-wake --now --key <KEY>
```

See `docs/architecture.html` for the picture of the original single-orchestrator line (Korean); the ticket orchestrator, judge and dream layers above are described in `SKILL.md` and `docs/history.md`.

| Lane | Who | Does | Never does |
|---|---|---|---|
| script | guard, idle, triage rules, verifiers, hold, locks | lock/dedupe/idle gate, deterministic judgements | judge prose |
| cheap model | waker, shadow classifier (`TRIAGE_MODEL`) | wake the line, classify one comment at a time (logged only until `enforce`) | change state |
| top orchestrator | `ORCHESTRATOR_*`, high reasoning | pick keys, take leases, launch ticket orchestrators, report, release the lock | edit code, push, deploy |
| ticket orchestrator | `TICKET_ORCHESTRATOR_*` | one key: next unit, brief, requirements, read verifier JSON, release the lease | edit code, touch other keys |
| workers | `sol` (codex) / `opus` (claude), `WORKER_*` | repro, fix, dev deploy, Jira evidence | widen the brief, touch the lock |
| judge | `JUDGE_*`, read-only tools | settle low-risk choices, staging approval after reporter/QA confirmation, `human-risk` questions with a suggested answer | security plans, scope, production data, VPN/OTP |
| dreamer | `DREAM_*` | once a day, curate the sdlc-kit memory (`sdlc.sh dream`) | touch tickets |
| human | the operator | VPN/OTP, risky decisions, judgement answers, secrets and permissions | routine relaying |

## New since the first public cut

- **Parallel ticket orchestrators.** The top orchestrator only hands out keys. Each key gets a lease (`tickets/<KEY>.json`, `launching` → `live`) and its own ticket orchestrator; `$JIRA_FOLLOWUP_HOME/lanes` sets how many run at once (no file = 5, `0` = the old sequential drain). Events for a leased key go to its inbox (`tickets/<KEY>.inbox.jsonl`).
- **Decision inbox.** Decisions open either as a local one-click page (`html`) or as items in the sdlc-kit to-do app (`app`, `tui`, `web`). Set it with `$JIRA_FOLLOWUP_HOME/decision-surface` or `JIRA_DECISION_SURFACE`. In the app you answer by reply or button: an option number, `승인` (approve), `보류` (hold), or a free sentence that is passed to the line as an instruction. Staging approvals are grouped in one item. `decision_replies.py` polls replies every 60 seconds.
- **Human-wait format.** Every `human-risk` wait carries one plain question and a suggested answer (or a human action line), so the app shows exactly what to decide. Things the line has no right to do are recorded once with `state_update.py refuse`.
- **Judge agent** (`judge.py`). An independent read-only Opus run that first looks for the strongest objection, follows the operator's past overrides, and decides only at low risk and confidence ≥ 0.8. Mode `off|shadow|on` lives in `$JIRA_FOLLOWUP_HOME/judge` (default `on`), toggled from a pinned to-do item or `judge.py set`. Everything it decides is logged in `judge.jsonl` and `decisions.jsonl` (`by=judge`); `judge.py replay` compares it with past human decisions.
- **Factory hold** (`jira-factory-hold set --until <ISO> [--only <statuses>] [--freeze stg,<statuses>] --reason <text>`). Until the expiry, only allowed statuses start new work, or the staging step and given statuses stop. Guard, ranking, leases, workers and staging approval requests all enforce it.
- **Lessons.** `state_update.py lesson` with a class from `scripts/lesson_classes.json`; repeats open a decision; a `fix-now` answer is tracked as a `[공장 수정]` to-do item until `state_update.py lesson-fixed`.
- **Ship gate.** `lifecycle.py check-ship` runs `sdlc.sh check ship` before the first push and dev deploy. Development phase comes from sdlc-kit; delivery phase from the queue's `delivery` field.
- **Minimal agent environment** (`factory_env.py`, on by default). Unattended Claude agents start without auto-memory and slow session hooks; the judge also runs without MCP and skills. Turn off with `echo off > $JIRA_FOLLOWUP_HOME/min-env`.
- **Dream.** On an idle tick, once a day, `dream_tick.py` starts one dreamer when `sdlc.sh dream --check` says memory cleanup is due. Turn off with `echo off > $JIRA_FOLLOWUP_HOME/dream`.
- **`wait-for`.** One condition (`--worker`, `--grep`/`--fail-grep`, `--file`, `--pid-gone`, `--port`, `--url`, `--cmd`) polled every second instead of `sleep` loops. Exit 0 success, 3 timeout, 4 failure.
- **Working marker.** While a worker runs on a key, the matching to-do item shows "working" (`todo working`, sdlc-kit v2.26+).
- **Chat-room watcher** (`teams_room.py`, optional). Reads one Microsoft Teams room through a chat CLI you provide, turns messages that call you into events, drafts replies and sends only approved drafts. The line never notifies through chat; alerts go to the decision surface.
- **Cluster checks** (`kube_pods.py`, optional). Cluster access preflight and a pod regex for filtering staging logs, through a connect command you provide.

## Relation to dark-factory

[cskwork/dark-factory](https://github.com/cskwork/dark-factory) is the **portable Python harness** version of the same loop: factory-wake / factory-guard / orchestrator / verifiers / decision pages, independent of any tracker or agent product. This repository is the **agent-skill form**: a Jira + git + Jenkins + Paseo flavoured runbook that an agent follows directly, with the scripts it calls. Use dark-factory to build your own line; use this skill if your stack already looks like this one.

## Requirements

- Python 3.11+ (standard library only), bash, git, node (for the to-do app and `todo_item.mjs`)
- [`acli`](https://developer.atlassian.com/cloud/acli/) logged in to your Jira site (`acli jira auth login`)
- `jk` (Jenkins CLI) contexts for dev and stg (deploy verification)
- Paseo (or any scheduler able to run `jira-daily-wake`, plus an agent runner with the same `run` flags) and the `claude` CLI for the judge
- Companion skills: [`jira-comment-format`](https://github.com/cskwork/jira-comment-format-skill) (Jira writing, `.sql`/`.txt` attachments, `jira_status_check.py`), [`sdlc-kit`](https://github.com/cskwork/sdlc-kit) (intent → plan → build → evidence; v2.15+ for the to-do decision surface, v2.26+ for pinned items, buttons and working markers, v2.27+ for the `Memory:` close rule, `dream` for memory cleanup); optionally [`browser-qa`](https://github.com/cskwork/browser-qa) for screen evidence
- Your own equivalents of the roles the RUNBOOK names in angle brackets: `<배포 스킬>` (deploy), `<로컬 기동 스킬>` (local run), `<시험 계정 스킬>` (test accounts), `<DB 조회 스킬>` (read-only DB), `<로그 조회 스킬>` (log search), `<토큰 발급 스킬>` (dev API token), `<기획 정본 저장소>` (planning source of truth), `<시나리오 실행기 스킬>` (browser scenario runner), and optionally `<채팅 CLI 스킬>` (chat CLI) and `<kube 접속 스킬>` (cluster access)

## Setup

```bash
git clone <this repo> ~/.agents/skills/jira-followup
mkdir -p ~/.jira-followup
cp ~/.agents/skills/jira-followup/config.example.env ~/.jira-followup/config.env
$EDITOR ~/.jira-followup/config.env        # site, project, accountId, statuses, branches, workspace

# entry points on PATH
for f in jira-daily-wake jira-daily-guard jira-worker jira-followup-approve decision-request \
         stg-approval-request jira-factory-hold wait-for; do
  ln -sfn ~/.agents/skills/jira-followup/scripts/$f ~/.local/bin/$f
done

# decision surface: app|tui|web (sdlc-kit to-do) or html (local page, the default)
echo app > ~/.jira-followup/decision-surface
```

Schedule one tick every 30 minutes on working days, for example cron `0,30 9-18 * * 1-5`. Run it in the workspace directory with the prompt *"Run `jira-daily-wake` only (timeout ≥ 600000 ms) and answer with its last line."* Use a cheap model for that prompt.

Day-to-day commands:

```bash
jira-daily-wake --now --key PROJ-123                        # run now for one key
jira-followup-approve PROJ-123 stg "<approval text>"        # staging approval from the CLI
jira-daily-wake --refill                                    # fill free ticket slots now
echo 3 > ~/.jira-followup/lanes                             # parallel ticket orchestrators (0 = sequential)
python3 scripts/run_lock.py lease-list --dir ~/.jira-followup
jira-factory-hold set --until 2026-12-01T18:00+09:00 --freeze stg --reason "release week"
jira-factory-hold clear
python3 scripts/judge.py set shadow                         # on | shadow | off
echo off > ~/.jira-followup/min-env                         # back to the full agent environment
echo off > ~/.jira-followup/dream                           # stop daily memory cleanup
```

## Configuration

All settings live in `$JIRA_FOLLOWUP_HOME/config.env` (default `~/.jira-followup/config.env`, outside the repo). Environment variables override it. Python scripts read it through `scripts/followup_config.py` and shell entry points load it themselves. `config.example.env` documents every key. The main groups:

| Group | Keys |
|---|---|
| Jira | `JIRA_SITE`, `JIRA_PROJECT`, `JIRA_ACCOUNT_ID`, `JIRA_EMAIL`, `JIRA_TOKEN_FILE` / `JIRA_API_TOKEN`, `JIRA_REAUTH_CMD` |
| Workflow | `STATUS_ORDER`, `STATUS_ON_DEV/STG/PROD`, `STATUS_DEV_DONE`, `STATUS_QA_DONE`, `STATUS_DONE_NOT_RELEASED`, `STATUS_POST_RELEASE`, `STATUS_TODO`, `SPINOFF_ISSUE_TYPE` |
| Git | `JIRA_FOLLOWUP_WORKSPACE`, `REPO_GLOB`, `SERVICE_REPO_PREFIX`, `BRANCH_DEV`, `BRANCH_STG`, `BRANCH_MAIN`, `MIGRATION_GLOB`, `RETRO_REPO` |
| Environments | `VPN_STATUS_CMD`, `DB_QUERY_CMD`, `DB_TOOL_DIR`, `DB_PRIMARY`, `DB_CONFIG_SUFFIX_*`, `DB_HOST_PREFIX_*`, `DB_SERVICE_MAP`, `*_HEALTH_URL_*`, `JK_CONTEXT_DEV`, `KUBE_CONNECT_CMD`, `KUBE_HOST_DEV/STG` |
| Agents | `PASEO_BIN`, `PASEO_ONCE_BIN`, `JIRA_FACTORY_WORKSPACE`, `ORCHESTRATOR_*`, `TICKET_ORCHESTRATOR_*`, `WORKER_SOL_*`, `WORKER_OPUS_*`, `JUDGE_*`, `DREAM_*`, `TRIAGE_MODEL` |
| Decisions | `JIRA_DECISION_SURFACE` (or the `decision-surface` file), `LINE_TITLE`, `FOLLOWUP_IDLE_NOTIFY_CMD` |
| Chat (optional) | `TEAMS_ROOM_CHAT_ID`, `TEAMS_ROOM_NAME`, `TEAMS_ME_MRI`, `TEAMS_ME_NAME`, `TEAMS_ME_NAMES`, `TEAMS_SELF_CHAT_ID`, `TEAMS_NOTIFY_CHAT_ID`, `TEAMS_NATIVE_CLI`, `CHAT_RECONNECT_HINT`; or your own watcher/sender via `FOLLOWUP_IDLE_TEAMS_CMD` / `CHAT_SEND_CMD` |
| Optional | `DEV_NOTE_*` (dev-note board), `COMMENT_FORMAT_DIR`, `SDLC_KIT` |

Runtime switches are small files in `$JIRA_FOLLOWUP_HOME`: `lanes`, `decision-surface`, `judge`, `min-env`, `dream`, `hold.json` (written by `jira-factory-hold`), `triage-mode`.

Credentials never live in the repo. Use `acli` and `jk` logins, a token file path, or exported environment variables. State files are kept under `$JIRA_FOLLOWUP_HOME`, outside git: `state.json`, `followup-queue.json`, `approvals.json`, `tickets/`, `locks/`, journals, `decisions.jsonl`, `judge.jsonl`, `lessons.jsonl`, `runs/`.

## Tests

```bash
cd scripts && python3 -m unittest discover -s tests
python3 -m pytest tests/test_decision_reduction.py    # function-style module, pytest only
```

The tests run offline. Jira, git, Jenkins, DB, chat, the to-do app, the judge model and HTTP are injected or faked. `tests/_harness.py` is loaded first (by `test_000_harness.py` under `discover`, by `conftest.py` under pytest); it clears inherited jira-followup settings, points the loader at an empty config, and blocks the real to-do file, chat CLI and judge. When you run one module directly, put the harness first: `python3 -m unittest test_000_harness <module>`.

## Not included

- **Error-signature log probe.** It depended on a private log-query tool; the RUNBOOK calls your `<로그 조회 스킬>` instead.
- **Chat CLI.** `teams_room.py` drives a chat CLI you provide (`TEAMS_NATIVE_CLI`, JSON contract in the script header); none is shipped here.
- **Browser scenario runner.** The RUNBOOK names `<시나리오 실행기 스킬>`; bring your own.
- **Architecture PNG renderings.** They contained internal identifiers; `docs/architecture.html` is the sanitized version and still shows the single-orchestrator line.

## License

MIT, see [LICENSE](LICENSE).
