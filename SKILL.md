---
name: jira-followup
description: Jira 자동 follow-up 단일 스케줄(예: 평일 08:00~18:30, 30분)의 정본. 아침 점검(내 담당 티켓 상태·배포 반영·보고)과 매 실행 멘션 follow-up(나를 멘션하거나 내 담당 티켓의 새 댓글, 내 담당 백로그 티켓 → sdlc-kit 의도 90% 판정, 미만이면 관찰 가능한 공백부터 재현하고 기대 동작은 요청자에게 질문 → 로컬 AS-IS/TO-BE 재현 → 수정 → dev 배포 → Jira 증적 댓글, 스테이징 배포만 승인 대기)을 한다. Use when "Jira follow-up 자동화", "멘션 자동 응답", "아침 점검 스케줄", "스테이징 배포 승인", "jira-followup-approve", follow-up 스케줄 동작 확인·수정 요청이 올 때.
---

# jira-followup

스케줄 1개가 이 폴더의 `RUNBOOK.md`를 그대로 따른다. 규칙을 바꿀 때는 `RUNBOOK.md`만 고친다. 명령은 `~/.local/bin`의 심링크로 이 폴더의 스크립트를 가리킨다.

구조도: [`docs/architecture.html`](docs/architecture.html). 판정은 싼 순서로 한다. 스크립트(모델 없음) → `gpt-6-luna`(깨우기·섀도 분류) → 할 일이 있을 때만 `claude-opus-5-5` high 오케스트레이터(`ORCHESTRATOR.md`, 지시·보고만) → 작업자(`gpt-6.1-sol` high / `claude-opus-5-5` medium) → 사람(VPN OTP·stg 승인) 순서다. 모델은 모두 `config.env`로 바꿀 수 있는 기본값이다. 완료는 결정적 검증기가 판정한다.

## 구성

| 파일 | 런타임 경로(심링크) | 역할 |
|---|---|---|
| `RUNBOOK.md` | (작업자가 읽음) | 실행 절차와 권한의 정본. §0 모드·허용 쓰기, §1~§6 아침 점검, §7 follow-up |
| `ORCHESTRATOR.md` | (오케스트레이터가 읽음) | 누가 RUNBOOK의 어느 절을 맡는지: 작업 단위·작업자 배정·brief·결과 확인·보고 |
| `scripts/followup_config.py` | (모든 스크립트가 import) | 설정 로더. 환경변수 → `$JIRA_FOLLOWUP_HOME/config.env`(기본 `~/.jira-followup/config.env`, git 밖) 순. 프로젝트 키·accountId·사이트·상태 이름·브랜치 이름·DB 접두어 등 |
| `scripts/jira-daily-wake` | `~/.local/bin/jira-daily-wake` | 스케줄러가 실행하는 tick 진입점. 인자 없이 부르면 가드 → 오케스트레이터 실행을 별도 세션으로 떼어 내고 바로 `wake=detached pid=<PID> log=<PATH>`를 돌려준다(접수일 뿐 완료가 아니다. 깨우기 모델의 exec가 짧은 시간 제한으로 빈 출력을 받고 끝나도 tick이 기록 없이 사라지지 않게 한다). 분리 실행과 장부는 `scripts/wake_supervisor.py`가 맡는다. 출력은 `$JIRA_FOLLOWUP_HOME/logs/wake-YYYYMMDD.log`, tick 시작·끝은 `ticks.log`(장부가 생기기 전 슬롯은 lost로 보지 않는다). `--now [--key]`는 동기 실행, `JIRA_WAKE_FOREGROUND=1`은 시험용 동기 실행. 실패하면 잠금 해제 |
| `scripts/jira-worker` | `~/.local/bin/jira-worker` | `jira-worker <sol\|opus> <title> <brief>`: 작업 한 단위를 맡기고 `worker-result.schema.json` 모양 JSON을 받음. `sol`=codex 작업자, `opus`=claude 작업자(`WORKER_*` 설정) |
| `scripts/jira-daily-guard` | `~/.local/bin/jira-daily-guard` | 중복·겹침 방지, `mode=morning|tick` 출력. tick에서 할 일이 없으면 모델 실행 전에 `skipping duplicate (idle...)`로 끝낸다 |
| `scripts/run_lock.py` | (가드·wake·worker가 호출) | 토큰 기반 실행 잠금·생존 판정·회수. 같은 슬롯 중복 방지 도우미(`PASEO_ONCE_BIN`)는 있으면 쓰고 없으면 건너뛴다 |
| `scripts/followup_idle.py` | (가드가 호출) | 스캔·큐·승인·env 대기를 보고 idle 여부 판정. exit 0 idle, 1 진행, 2 오류(진행). 채팅 감시는 `FOLLOWUP_IDLE_TEAMS_CMD`가 있을 때만 |
| `scripts/followup_triage.py` | (idle이 호출) | `answeredByMeAfter` 결정 처리(handled `human`), luna 한 건씩 분류. `triage-mode` 기본 `shadow`, `--eval`·`--report`, `--check-intent` 재현 우선·질문 게시 전 검증 |
| `scripts/followup_scan.py` | (가드·작업자가 호출) | 72시간 새 댓글 중 멘션·담당 이벤트 + 진행 중 티켓의 미답변 멘션(`backstop`) + 내 담당 프로젝트 시작 전 티켓의 백로그 접수(`intake`) + sdlc-kit to-do 답 추출(읽기 전용) |
| `scripts/state_update.py` · `drain.py` | (모두) | 상태 파일 키 단위 patch(flock + `os.replace`), 순차 drain 예산·순위·검증 |
| `scripts/morning_check.py` | (§1~§6) | 고정 아침 점검: 수집·배포 이력·상태 이동 계획·되읽기·보고. 기본 dry-run, `--execute`만 상태·노트 보드 반영 |
| `scripts/session_digest.py` | (RETRO §1) | `--since <lastRetroAt>` 세션별 사용자 정정·도구 오류·훅 차단 입력. 공장 세션은 축약 |
| `scripts/preflight_env.py` | (§7-3-0) | 다음 동작 `--queue-item` needs 계산, VPN·DB `@@hostname`·AI·웹·Jira·Jenkins 점검. exit 0/10 사람/20 장애 |
| `scripts/ticket_refs.py` | (§2, §7-3-2) | 커밋 제목 `[KEY]`로 저장소별 위치·운반 브랜치·함께 올라가는 키·dev/stg/main 반영을 판정. `own-branch` / `carried` / `none` |
| `scripts/decision-request` + `decision_page.py` | `~/.local/bin/decision-request` | 사람 결정 페이지(로컬 브라우저). `stg-approval <KEY>` / `spec <json>`(sql-run·choice·teams-reply). 버튼 클릭 = 결정, `decisions.jsonl` 감사 기록, 바로 `--now --key` 실행 |
| `scripts/stg-approval-request` | `~/.local/bin/stg-approval-request` | 스테이징 DB 마이그레이션 확인(`check_migrations.py`) 뒤에만 stg 승인 페이지를 연다 |
| `scripts/sql_runner.py` | (결정 페이지가 호출) | 승인된 SQL 파일을 dev/stg에 실행. 같은 연결 `@@hostname` 접두어·guard 확인, 첫 오류에서 중단 |
| `scripts/check_base.py` | (§7-3-2, 서비스 pre-push 훅) | `origin/main..브랜치`가 티켓 커밋만인지(병합 커밋·다른 키 FAIL). 병합 커밋 위에서는 `--feature-side` |
| `scripts/check_merge_msg.py` | (§7-3-4) | 승격 병합 커밋 제목 형식 `merge(<서비스>): <요약> -> <대상> [KEY]` 확인 |
| `scripts/check_deploy.py` | (§7-3-4·6) | Jenkins 빌드 완료·SUCCESS·체크아웃 커밋이 기대 커밋을 포함하는지. `--serving-json`이면 서비스 Deployment 상태를 `serving`으로 따로 판정 |
| `scripts/check_migrations.py` | (§7-3-6) | 승격 범위의 마이그레이션이 스테이징 DB에 적용됐는지 읽기 전용으로 확인 |
| `scripts/runbook_section.py` | (`jira-worker`가 호출) | 브리프가 인용한 `RUNBOOK §7-3-4`·`ORCHESTRATOR §2` 같은 절만 뽑아 작업자 프롬프트에 붙임(전체 파일 재독 대신) |
| `scripts/spinoff_ticket.py` | (§7-2 운영 후속) | 운영 완료 티켓 댓글의 반영 이슈 → 원 티켓과 `Relates`로 이은 새 티켓(담당 나, 할 일) + 원 티켓 안내 댓글 + 되읽기. 작성자 직발행 예고·댓글 뒤 연결 티켓이면 만들지 않음. exit 0/1/2/3/4/5, 재실행은 남은 단계만 |
| `scripts/dev_note_sync.py` | (`morning_check.py`가 호출) | (선택) 아침 보고서 → 개발자 노트 보드 1건. `DEV_NOTE_*` 계약은 동반 스킬 [jira-comment-format](https://github.com/cskwork/jira-comment-format-skill)의 `dev_note.py`와 같다 |
| `scripts/followup_note_detail.py` | (§7-7) | (선택) 이번 실행 일지 줄 → 노트 보드용 하위 목록. `RUN_REPORT_CMD`로 넘긴다 |
| `scripts/jira-followup-approve` | `~/.local/bin/jira-followup-approve` | 스테이징 배포 승인 기록 |
| `RETRO.md` | (주간 스케줄) | `lessons.jsonl`로 주간 개선안을 `retro/YYYYMMDD` 브랜치로 제안. 병합은 사람 |

상태(git 밖): `$JIRA_FOLLOWUP_HOME/{config.env, state.json, followup-queue.json, approvals.json, followup-YYYY-MM-DD.md, YYYY-MM-DD.md, triage-mode, triage-shadow.jsonl, teams-scan.json, decisions.jsonl, runs/, logs/}`. 채팅 감시·발송 도구(선택)는 이 스킬에 없다. 쓰려면 `FOLLOWUP_IDLE_TEAMS_CMD`(scan)·`CHAT_SEND_CMD`(승인된 초안 발송)에 연결한다(RUNBOOK §7-8).

시험: `cd scripts && python3 -m unittest discover -s tests` (오프라인).

## 권한 경계 (RUNBOOK §0이 정본)

- 자동: 의도 판정, 로컬 AS-IS/TO-BE 재현, `fix/<KEY>` 수정·push, dev 브랜치 병합·Jenkins dev 배포·스모크, Jira 상태 이동, 새 증적 댓글, 운영 후속 티켓 생성(`spinoff_ticket.py`만).
- 승인 대기: stg 브랜치 push·Jenkins stg 배포. `approvals.json`의 미사용 승인만 인정한다.
- 승인 대기: 채팅 답장 발송. 자동은 방 읽기·초안·결정 페이지까지이고, `teams-reply` 승인 버튼을 누른 초안만 한 번 보낸다. 채팅 메시지는 stg 승인이 아니다.
- 금지: 운영(main 브랜치·태그·운영 잡·운영 DB), force push, 기존 댓글 수정·삭제, 담당자·필드 변경.

Jira 댓글 서식(기획자용 틀, 개발자 확인용 읽기 전용 쿼리는 `.sql` 첨부, 되읽기 검사)은 동반 스킬 `jira-comment-format`을 따른다(RUNBOOK §7-3-5).

## 사용

```bash
jira-daily-wake --now --key PROJ-123                 # 수동 실행: tick을 기다리지 않음(25분 창·idle 무시, 실행 중 잠금은 지킴)
jira-followup-approve PROJ-123 stg "스테이징 배포 진행"  # 스테이징 배포 승인 → 다음 실행이 진행
jq . "$JIRA_FOLLOWUP_HOME/followup-queue.json"           # 진행·대기 티켓
tail "$JIRA_FOLLOWUP_HOME/followup-$(date +%F).md"       # 오늘 처리 기록
```

## 새 PC 설치

1. 설정: `mkdir -p ~/.jira-followup && cp config.example.env ~/.jira-followup/config.env` 후 값을 채운다(다른 위치는 `JIRA_FOLLOWUP_HOME`). 비밀값은 넣지 않는다. Jira 토큰은 `JIRA_TOKEN_FILE`이 가리키는 파일이나 환경변수에 둔다.
2. 심링크: 위 표의 `~/.local/bin` 경로를 이 폴더 스크립트로 `ln -sfn` 한다. `acli` 인증, `jk` context(dev·stg), sdlc-kit(`$JIRA_FOLLOWUP_WORKSPACE/.sdlc/config.md`의 `kit:`, `human_gates:` 비움), 실행기 `paseo`(또는 `PASEO_BIN`)가 필요하다.
3. 스케줄: Paseo 스케줄이나 cron 등으로 `jira-daily-wake`를 부른다. 예: cron `0,30 8-18 * * 1-5`, 시간대 `Asia/Seoul`, cwd는 `$JIRA_FOLLOWUP_WORKSPACE`. 깨우기 모델을 쓰는 스케줄러면 싼 모델(예: `gpt-6-luna` medium)에 "`jira-daily-wake`만 실행(timeout 600000 ms 이상)하고 마지막 줄로 답"만 시킨다. 주간 회고는 `RETRO.md`를 따르는 별도 스케줄로 둔다.

## 함정 (실측)

- JQL로는 ADF 멘션을 찾을 수 없다(`comment ~`, `watcher`로 멘션 티켓이 잡히지 않음). 스캐너는 `updated >=`로 받아 댓글의 mention `attrs.id`로 거른다.
- `acli workitem search --fields key`만 주면 결과가 모두 null이다. `key,summary`를 준다.
- 아침 §5에서 `state.json`을 저장할 때 `followup` 키를 보존해야 한다. 덮어쓰면 이미 답한 댓글에 다시 답한다.
- `jk`는 JSON을 출력한 뒤 약 60초를 더 기다렸다가 끝난다. 검증기의 jk timeout은 120~150초다.
- dev와 스테이징 DB가 같은 VIP를 쓰면 스테이징 설정으로 조회해도 VPN이 dev면 dev가 답한다. `preflight_env.py`가 `@@hostname` 접두어(`DB_HOST_PREFIX_*`)로 잡아 `env-human`으로 돌린다.
- luna 분류는 한 건씩, 수신자 맥락(`mentionsMe`·`assignedToMe`)과 함께 보낸다. 여러 건을 묶으면 놓침이 생기고, 한 건씩 + 수신자 맥락이면 초기 표본에서 놓침이 없었다. 그래도 `enforce`는 섀도 데이터가 쌓인 뒤 운영자 승인으로만 켠다.
- 잠금 해제는 `jira-daily-guard --release`로 한다. 실행기가 직접 `rm -f`를 쓰면 자동 승인 검토가 거부해 errors가 난다.
- 아침 fetch 대상은 `REPO_GLOB`에 맞고 `origin` 원격이 있는 저장소뿐이다. 원격 없는 로컬 도구 저장소를 세면 errors 대부분이 거기서 나온다.
- idle 차단은 모델 토큰만 줄인다. 실행 시간(약 3분)의 대부분은 acli 스캔이다.
