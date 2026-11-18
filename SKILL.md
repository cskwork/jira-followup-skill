---
name: jira-followup
description: Jira 자동 follow-up 단일 스케줄(예: 평일 08:00~18:30, 30분)의 정본. 아침 점검(내 담당 티켓 상태·배포 반영·보고)과 매 실행 멘션 follow-up(나를 멘션하거나 내 담당 티켓의 새 댓글, 내 담당 백로그 티켓 → sdlc-kit 의도 90% 판정, 미만이면 관찰 가능한 공백부터 재현하고 기대 동작은 요청자에게 질문 → 로컬 AS-IS/TO-BE 재현 → 수정 → dev 배포 → Jira 증적 댓글, 스테이징 배포만 승인 대기)을 한다. Use when "Jira follow-up 자동화", "멘션 자동 응답", "아침 점검 스케줄", "스테이징 배포 승인", "jira-followup-approve", "공장 일시정지", "판정 에이전트", follow-up 스케줄 동작 확인·수정 요청이 올 때.
---

# jira-followup

스케줄 1개가 이 폴더의 `RUNBOOK.md`를 그대로 따른다. 규칙을 바꿀 때는 `RUNBOOK.md`만 고친다. 명령은 `~/.local/bin`의 심링크로 이 폴더의 스크립트를 가리킨다.

구조도: [`docs/architecture.html`](docs/architecture.html). 판정은 싼 순서로 한다. 스크립트(모델 없음) → `gpt-6-luna`(깨우기·섀도 분류) → 할 일이 있을 때만 `claude-opus-5-5` high 상위 오케스트레이터(`ORCHESTRATOR.md`, 배분·보고만) → 키마다 티켓 오케스트레이터(`TICKET-ORCHESTRATOR.md`, opus high, 최대 `lanes`개 동시) → 작업자(`gpt-6.2-sol` high / `claude-opus-5-5` medium) → 사람(VPN OTP·stg 승인) 순서다. 위험이 낮은 사람 결정은 판정 에이전트(judge)가 먼저 본다. 모델은 모두 `config.env`로 바꿀 수 있는 기본값이다. 완료는 결정적 검증기가 판정한다.

감시 상태·실행 checkpoint·호출자 필수 검증·실측 summary 계약: [런타임 설명](docs/retrospective-runtime.md).

## 구성

| 파일 | 런타임 경로(심링크) | 역할 |
|---|---|---|
| `RUNBOOK.md` | (작업자가 읽음) | 실행 절차와 권한의 정본. §0 모드·허용 쓰기(§0-1 상태 쓰기·일지·lesson·sdlc 메모리, §0-2 사람 대기 형식, §0-3 묻지 않는 절차: 모든 역할 공통), §1~§6 아침 점검, §7 follow-up(§7-6은 오케스트레이터 전용, §7-H 일시정지) |
| `ORCHESTRATOR.md` | (상위 오케스트레이터가 매 tick 읽음) | 핵심: 역할·입력·소유권·단위 표(§2-A)·티켓 배분(§2-0)·상태 쓰기(§2-2)·보고(§5). 15KB 안팎으로 유지한다(시험이 크기를 지킨다) |
| `ORCHESTRATOR-REFERENCE.md` | (상황이 생길 때 절만) | 참조: 회수(§1-2)·순차 drain(§2-1)·브랜치 판정·병합 충돌(§2-3)·brief(§3)·결과 판정(§4)·결정 spec(§5-1). 핵심과 절 번호를 함께 쓴다(`runbook_section.py`가 두 파일을 같이 찾는다) |
| `TICKET-ORCHESTRATOR.md` | (티켓 오케스트레이터가 읽음) | 키 하나를 RUNBOOK §7-3 단계대로: packet 먼저, inbox·다음 단위·자원·멈춤·임대 해제 |
| `scripts/followup_config.py` | (모든 스크립트가 import) | 설정 로더. 환경변수 → `$JIRA_FOLLOWUP_HOME/config.env`(기본 `~/.jira-followup/config.env`, git 밖) 순. 프로젝트 키·accountId·사이트·상태 이름·브랜치 이름·DB 접두어 등 |
| `scripts/jira-daily-wake` | `~/.local/bin/jira-daily-wake` | 스케줄러가 실행하는 tick 진입점. 인자 없이 부르면 가드 → 오케스트레이터 실행을 별도 세션으로 떼어 내고 바로 `wake=detached pid=<PID> log=<PATH>`를 돌려준다(접수일 뿐 완료가 아니다. 깨우기 모델의 exec가 짧은 시간 제한으로 빈 출력을 받고 끝나도 tick이 기록 없이 사라지지 않게 한다). 분리 실행과 장부는 `scripts/wake_supervisor.py`가 맡는다. 출력은 `$JIRA_FOLLOWUP_HOME/logs/wake-YYYYMMDD.log`, tick 시작·끝은 `ticks.log`(장부가 생기기 전 슬롯은 lost로 보지 않는다). `--now [--key]`는 동기 실행, `--refill`은 빈 티켓 슬롯 채우기, `JIRA_WAKE_FOREGROUND=1`은 시험용 동기 실행. 실패하면 잠금 해제 |
| `scripts/jira-ticket-run` | (상위가 호출) | 티켓 임대 토큰으로 티켓 오케스트레이터(label `role=ticket-orchestrator run=<tt> key=<K>`)를 띄우고 agentId를 출력 |
| `scripts/ticket_packet.py` | (티켓 오케스트레이터가 처음 읽음) | 키 하나의 요약 JSON: 큐 항목(note 끝부분), `delivery`·sdlc, 승인, 최근 결정 3줄, 임대, 처리 안 된 inbox, 그 키 handled, 최근 requirements, 전체 파일 경로. 읽기 전용 |
| `scripts/factory_workspace.py` | (`jira-daily-wake`·`jira-ticket-run`이 호출) | 공장 에이전트가 함께 쓰는 고정 Paseo workspace ID(`factory-workspace`). 실행마다 workspace가 쌓이지 않게 한다. 실패하면 workspace 없이 예전처럼 띄운다 |
| `scripts/jira-worker` | `~/.local/bin/jira-worker` | `jira-worker <sol\|opus> <title> <brief>`: 작업 한 단위를 맡기고 `worker-result.schema.json` 모양 JSON을 받음. `sol`=codex 작업자, `opus`=claude 작업자(`WORKER_*` 설정). `--requirements`는 호출자 선언 검증, `--resources`는 자원 잠금, `--key`가 있으면 도는 동안 그 키의 할 일 항목에 작업 중 표시(`todo working`, RUNBOOK §7-3-L) |
| `scripts/resource_lock.py` | (`jira-worker --resources`가 호출) | `runtime-local`·`deploy-<service>` 자원 잠금(RUNBOOK §7-3-L). 대기 마감 초과는 exit 4 |
| `scripts/wait-for` | `~/.local/bin/wait-for` | `sleep` 대기 루프 대신 조건 하나(`--worker`·`--grep`+`--fail-grep`·`--file`·`--pid-gone`·`--port`·`--url`·`--cmd`)를 1초 간격으로 기다림. exit 0 성공·3 시간 초과·4 실패, 출력 한 줄 |
| `scripts/jira-daily-guard` | `~/.local/bin/jira-daily-guard` | 중복·겹침 방지, `mode=morning|tick` 출력. tick에서 할 일이 없으면 모델 실행 전에 `skipping duplicate (idle...)`로 끝낸다 |
| `scripts/run_lock.py` | (가드·wake·worker가 호출) | 토큰 기반 실행 잠금·생존 판정·회수, 티켓 임대(`lease-acquire`·`lease-list`·`lease-release`·`owns-ticket`). 같은 슬롯 중복 방지 도우미(`PASEO_ONCE_BIN`)는 있으면 쓰고 없으면 건너뛴다 |
| `scripts/followup_idle.py` | (가드가 호출) | 스캔·큐·승인·env 대기·hold를 보고 idle 여부 판정. exit 0 idle, 1 진행, 2 오류(진행). 채팅 감시는 채팅 방을 설정했을 때만 |
| `scripts/followup_triage.py` | (idle이 호출) | `answeredByMeAfter` 결정 처리(handled `human`), luna 한 건씩 분류. `triage-mode` 기본 `shadow`, `--eval`·`--report`, `--check-intent` 재현 우선·질문 게시 전 검증 |
| `scripts/followup_scan.py` | (가드·작업자가 호출) | 72시간 새 댓글 중 멘션·담당 이벤트 + 진행 중 티켓의 미답변 멘션(`backstop`) + 내 담당 프로젝트 시작 전 티켓의 백로그 접수(`intake`) + sdlc-kit to-do 답 추출·큐 대기 동기화 + 큐↔Jira 상태 대조 |
| `scripts/state_update.py` · `drain.py` | (모두) | 상태 파일 키 단위 patch(flock + `os.replace`, 토큰·키 쓰기 범위 확인), `lesson`·`refuse`·`lesson-fixed`, 순차 drain 예산·순위·검증·`check-result`·`summary-record` |
| `scripts/lifecycle.py` | (가드·judge·preflight·drain·`jira-worker` declare, §7-3) | 개발 단계=sdlc(`sdlc.sh status --json`, 읽기 전용), 배포 단계=큐 `delivery`(없으면 옛 `stage`에서 대응). `check-ship`은 첫 push·dev 배포 전 `sdlc.sh check ship` 게이트(exit 0/1, FAIL이면 human-risk 질문·권장 답). `JIRA_SDLC=off`면 sdlc를 부르지 않는다 |
| `scripts/morning_check.py` + `morning_track.py` | (§1~§6) | 고정 아침 점검: 수집·배포 이력·상태 이동 계획·되읽기·보고. 기본 dry-run, `--execute`만 상태·노트 보드 반영. 자동 이동은 `스테이징 검수 완료` 이상 + 운영 태그만, QA 단계 이동은 하지 않는다(§3). 의존성 실패는 오류로 세고, 3회 이어진 조치와 7일 넘은 미병합 `retro/*` 제안은 결정 하나로 올린다(RUNBOOK §4). dry-run은 공장 저장소를 fetch하지 않는다 |
| `scripts/session_digest.py` | (RETRO §1) | `--since <lastRetroAt>` 세션별 사용자 정정·도구 오류·훅 차단 입력. 공장 세션은 축약 |
| `scripts/preflight_env.py` | (§7-3-0) | 다음 동작 `--queue-item` needs 계산, VPN·DB `@@hostname`·AI·웹·Jira·Jenkins·클러스터 점검. exit 0/10 사람/20 장애 |
| `scripts/kube_pods.py` | (§7-3-6) | 스테이징 로그를 서비스 pod로 거르는 정규식 생성(현재·과거 ReplicaSet 포함), 클러스터 접근 사전 점검 |
| `scripts/ticket_refs.py` | (§2, §7-3-2) | 커밋 제목 `[KEY]`로 저장소별 위치·운반 브랜치·함께 올라가는 키·dev/stg/main 반영을 판정. `own-branch` / `carried` / `none` |
| `scripts/decision-request` + `decision_page.py` + `decision_inbox.py` | `~/.local/bin/decision-request` | 사람 결정. 결정 화면은 `$JIRA_FOLLOWUP_HOME/decision-surface`(또는 `JIRA_DECISION_SURFACE`, app|tui|web|html, 없으면 html) 하나만 쓴다. html이면 로컬 브라우저 결정 페이지(버튼 클릭 = 결정), 그 밖이면 sdlc-kit 할 일 '확인 필요' 항목 `[결정 D<n>] <KEY> · <제목>`(답글 = 결정: 번호, 실행 승인은 `승인`, `보류`). `stg-approval <KEY>` / `spec <json>`(sql-run·choice·teams-reply). choice는 `recommended`로 AI 권장 선택지를 표시. `decisions.jsonl` 감사 기록(`by=button|todo-app|judge`), 바로 `--now --key` 실행 |
| `scripts/decision_replies.py` | (`decision-request`·`jira-daily-wake`가 띄움) | 단일 수신기: 60초마다 할 일 결정 항목의 내 답글을 결정으로 기록하고 항목을 닫는다(`decision_inbox.poll`). judge 설정 항목을 반영하고 죽은 결정 페이지를 만료시킨다 |
| `scripts/stg-approval-request` | `~/.local/bin/stg-approval-request` | judge 판정(`judge.py stg`)과 스테이징 DB 마이그레이션 확인(`check_migrations.py`) 뒤에만 stg 승인 결정을 연다 |
| `scripts/judge.py` | (`decision-request`·`stg-approval-request`·가드가 호출) | 판정 에이전트. 독립 Opus(`JUDGE_*` 설정, 기본 `claude-opus-5-5` medium, 읽기 도구만)가 반대 근거를 먼저 찾은 뒤 위험이 낮은 결정만 정한다. 모드 `$JIRA_FOLLOWUP_HOME/judge`(off\|shadow\|on, 없으면 on, 깨졌으면 off). 할 일 앱 모든 탭 맨 위에 고정된 `[공장 설정] 판정 에이전트(judge): 켜짐|꺼짐` 줄의 `켜기 | 끄기` 버튼(답글)으로 바꾼다(결정 수신기가 60초마다 반영). 터미널은 `judge.py set on|off|shadow`. `choice`(humanReason policy·conflict만, security-plan·product-scope·prod-data는 사람), `stg`(RUNBOOK §7-3 6단계 승인 규칙), `risk`(질문·권장 답이 있는 human-risk, 키당 하루 2회), `replay`(지난 사람 결정 대조). 기록 `judge.jsonl`, 적용한 결정은 `decisions.jsonl` `by=judge` |
| `scripts/jira-factory-hold` + `factory_hold.py` | `~/.local/bin/jira-factory-hold` | 공장 일시정지(RUNBOOK §7-H): `set --until <ISO+09:00> [--only 상태,…] [--freeze stg,상태,…] --reason`·`clear`·`status [--key]`·`check --key`. `hold.json` 하나로 만료 시각까지 허용 상태만 처리하거나 stg 단계·특정 상태를 멈춘다. 가드·rank·임대·작업자·stg 승인 요청이 코드로 막는다 |
| `scripts/factory_env.py` | (`jira-daily-wake`·`jira-ticket-run`·`jira-worker opus`·`dream_tick.py`·`judge.py`가 호출) | 무인 Claude 최소 환경(min-env). 아래 [최소 환경과 메모리 정리](#최소-환경과-메모리-정리) |
| `scripts/dream_tick.py` | (`jira-daily-wake`가 idle tick 끝에 분리 실행) | 하루 한 번 sdlc 메모리 정리(dream) 에이전트. 아래 [최소 환경과 메모리 정리](#최소-환경과-메모리-정리) |
| `scripts/lessons.py` + `lesson_classes.json` + `lesson_prompt.py` | (`state_update.py lesson`·가드·작업자 프롬프트) | 교훈을 닫힌 분류(`class`)로 묶고, 14일 안 3회 반복이면 결정(`fix-now`·`accept`·`ignore`)을 연다. 최근 14일 요약을 오케스트레이터·작업자 프롬프트에 넣는다(RUNBOOK §7-6) |
| `scripts/sql_runner.py` | (결정 페이지가 호출) | 승인된 SQL 파일을 dev/stg에 실행. 같은 연결 `@@hostname` 접두어·guard 확인, 첫 오류에서 중단 |
| `scripts/check_base.py` | (§7-3-2, 서비스 pre-push 훅) | `origin/main..브랜치`가 티켓 커밋만인지(병합 커밋·다른 키 FAIL). 병합 커밋 위에서는 `--feature-side` |
| `scripts/check_merge_msg.py` | (§7-3-4) | 승격 병합 커밋 제목 형식 `merge(<서비스>): <요약> -> <대상> [KEY]` 확인 |
| `scripts/check_deploy.py` | (§7-3-4·6) | Jenkins 빌드 완료·SUCCESS·체크아웃 커밋이 기대 커밋을 포함하는지. `--serving-json`이면 서비스 Deployment 상태를 `serving`으로 따로 판정 |
| `scripts/check_migrations.py` | (§7-3-6) | 승격 범위의 마이그레이션이 스테이징 DB에 적용됐는지 읽기 전용으로 확인 |
| `scripts/runbook_section.py` | (`jira-worker`가 호출) | 브리프가 인용한 `RUNBOOK §7-3-4`·`ORCHESTRATOR §2` 같은 절만 뽑아 작업자 프롬프트에 붙임(전체 파일 재독 대신). RUNBOOK §0은 항상 붙이고, 오케스트레이터 전용 §7-6은 인용돼도 붙이지 않는다 |
| `scripts/spinoff_ticket.py` | (§7-2 운영 후속) | 운영 완료 티켓 댓글의 반영 이슈 → 원 티켓과 `Relates`로 이은 새 티켓(담당 나, 할 일) + 원 티켓 안내 댓글 + 되읽기. 작성자 직발행 예고·댓글 뒤 연결 티켓이면 만들지 않음. exit 0/1/2/3/4/5, 재실행은 남은 단계만 |
| `scripts/teams_room.py` | (가드가 `scan` 호출) | (선택) 설정한 Microsoft Teams 방(`TEAMS_ROOM_CHAT_ID`)에서 다른 사람이 나를 부른 메시지(`TEAMS_ME_NAMES` 호칭·내 MRI 멘션)를 앞선 5건과 함께 이벤트로 뽑는다(읽기 전용, 기준점 `teams-room.json`). `draft`는 답장 초안과 `teams-reply` 결정, `send`는 승인 줄이 있을 때만 한 번 발송·verify(RUNBOOK §7-8). 채팅 CLI는 `<채팅 CLI 스킬>`(`TEAMS_NATIVE_CLI`) |
| `scripts/dev_note_sync.py` | (`morning_check.py`가 호출) | (선택) 아침 보고서 → 개발자 노트 보드 1건. `DEV_NOTE_*` 계약은 동반 스킬 [jira-comment-format](https://github.com/cskwork/jira-comment-format-skill)의 `dev_note.py`와 같다 |
| `scripts/followup_note_detail.py` | (§7-7) | (선택) 이번 실행 일지 줄 → 노트 보드용 하위 목록. `RUN_REPORT_CMD`로 넘긴다 |
| `scripts/scratch_cleanup.py` | (사람이 실행, `--result`는 오케스트레이터) | 운영 완료 티켓의 `.scratch` worktree를 지운다(기본 dry-run, `--execute`만 `git worktree remove`, force 없음). `--result`는 작업자 결과의 `cleanup:` worktree만(규칙 정본 RUNBOOK §0 git 줄). `--runs`는 상태 폴더의 비토큰 `runs/` 항목과 맨 위 티켓 키 덤프를 보고만 한다 |
| `scripts/jira-followup-approve` | `~/.local/bin/jira-followup-approve` | 스테이징 배포 승인 기록 |
| `RETRO.md` | (주간 스케줄) | `lessons.jsonl`로 주간 개선안을 `retro/YYYYMMDD` 브랜치로 제안. 병합은 사람 |

상태(git 밖): `$JIRA_FOLLOWUP_HOME/{config.env, state.json, followup-queue.json, notes/<KEY>.md(큐 note 상한을 넘은 앞 기록), approvals.json, hold.json, lanes, judge, min-env, dream, dream-last.json, dream.log, judge.jsonl, judge-state.json, tickets/<KEY>.json, tickets/<KEY>.inbox.jsonl, locks/, decision-surface, decision-pages/{codes.json,<name>.spec.json}, decisions.jsonl, lessons.jsonl, factory-workspace, morning-carry.json, followup-YYYY-MM-DD.md, YYYY-MM-DD.md, triage-mode, triage-shadow.jsonl, teams-room.json, teams-scan.json, scan-state.json, teams-drafts/, runs/, logs/}`.

Jira 댓글 서식(기획자용 틀, 개발자 확인용 읽기 전용 쿼리는 `.sql` 첨부, 개발자용 상세는 `.txt` 첨부, 되읽기 검사, 상태 이동 판정 `jira_status_check.py`)은 동반 스킬 `jira-comment-format`을 따른다(RUNBOOK §7-3-5).

시험: `cd scripts && python3 -m unittest discover -s tests -v` (오프라인; 현재 수는 실행 결과로 확인). 실제 할 일·채팅·judge 차단은 `tests/_harness.py`(이름이 가장 앞서는 `test_000_harness`·pytest `conftest.py`가 켬)이고, 모듈 하나만 돌릴 때는 `python3 -m unittest test_000_harness <모듈>`처럼 앞에 둔다. 최상위 `def test_` 모듈(`test_decision_reduction`)은 `python3 -m pytest`로만 돈다.

## 권한 경계 (RUNBOOK §0이 정본)

- 자동: 의도 판정, 로컬 AS-IS/TO-BE 재현, `fix/<KEY>` 수정·push, dev 브랜치 병합·Jenkins dev 배포·스모크, 새 증적 댓글, 그 댓글을 증빙으로 한 Jira 상태 이동(`jira_status_check.py --evidence-comment`), 운영 후속 티켓 생성(`spinoff_ticket.py`만).
- 승인 대기: stg 브랜치 push·Jenkins stg 배포. `approvals.json`의 미사용 승인만 인정한다. judge 모드가 off가 아니면 judge가 승인을 쓸 수 있다: Jira가 마지막 dev 결과 댓글 뒤에 다른 사람(기획·QA)이 옮긴 `개발 검수 완료`(`STATUS_DEV_DONE`)이거나(shadow·on. 공장도 내 계정으로 옮기므로 내 계정의 전환은 사람 승인으로 둔다), 그 뒤 기획·QA 댓글이 확인 완료를 알릴 때(on)다. 아니면 보고자를 멘션해 `개발 검수 완료`로 옮겨 달라는 댓글을 결과 댓글마다 한 번 달고 기다린다. 본인이 보고한 티켓은 사람 승인이다.
- 승인 대기: 채팅 답장 발송. 자동은 방 읽기·초안·결정까지이고, `teams-reply`를 승인(결정 페이지 버튼 또는 할 일 답글 `승인`)한 초안만 한 번 보낸다. 채팅 메시지는 stg 승인이 아니다.
- 알림: 공장은 채팅으로 알리지 않는다. 결정·조치 필요·경보는 결정 화면 설정대로 할 일 '확인 필요'(html이면 데스크톱 알림·일지)로만 간다.
- 금지: 운영(main 브랜치·태그·운영 잡·운영 DB), force push, 기존 댓글 수정·삭제, 담당자·필드 변경.

## 사용

```bash
jira-daily-wake --now --key PROJ-123                 # 수동 실행: tick을 기다리지 않음(25분 창·idle 무시, 실행 중 잠금은 지킴)
jira-followup-approve PROJ-123 stg "스테이징 배포 진행"  # 스테이징 배포 승인 → 다음 실행이 진행
jq . "$JIRA_FOLLOWUP_HOME/followup-queue.json"           # 진행·대기 티켓
tail "$JIRA_FOLLOWUP_HOME/followup-$(date +%F).md"       # 오늘 처리 기록
python3 scripts/scratch_cleanup.py [--execute]           # 운영 완료 티켓 .scratch worktree 정리(기본 dry-run)
echo 5 > "$JIRA_FOLLOWUP_HOME/lanes"                     # 동시 티켓 오케스트레이터 수(파일 없음=5)
echo 0 > "$JIRA_FOLLOWUP_HOME/lanes"                     # 되돌리기: 상위 순차 drain(남은 임대는 끝날 때까지 유지)
jira-daily-wake --refill                                 # 빈 슬롯 채우기(25분 간격 무시, 전역 잠금이 바쁘면 그냥 끝남)
python3 scripts/run_lock.py lease-list --dir "$JIRA_FOLLOWUP_HOME"   # 진행 중 티켓 임대
jira-factory-hold set --until 2026-12-01T18:00+09:00 --freeze stg --reason "운영 반영 주간"  # 일시정지
python3 scripts/judge.py set off                         # 판정 에이전트 끄기(on|shadow)
wait-for --url http://localhost:8080/health --timeout 120  # sleep 루프 대신 조건 대기
```

## 최소 환경과 메모리 정리

- 최소 환경(min-env, 기본 켜짐): 공장이 띄우는 Claude 에이전트(오케스트레이터·티켓 오케스트레이터·`jira-worker opus`·dreamer)에 `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1`(자동 메모리 색인)과, 무인 세션을 느리게 하는 세션 훅 플러그인(메모리 훅·저장소 안내문)을 끄는 변수를 준다(목록은 `factory_env.py`의 `CLAUDE_ENV`). judge의 `claude -p`는 `--tools Read,Grep,Glob --strict-mcp-config --disable-slash-commands`로 도구·MCP·스킬 목록을 뺀다. 설정 출처는 그대로라 안전 훅(`~/.claude/settings.json`·프로젝트 `.claude` 훅·프로젝트 CLAUDE.md)은 그대로 실린다. sol(codex) 작업자는 바뀌지 않는다. 되돌리기: `echo off > "$JIRA_FOLLOWUP_HOME/min-env"`(첫 단어 `off`면 다음 실행부터 바로 예전 명령, 파일을 지우면 다시 켜짐).
- 메모리 정리(dream): tick이 할 일 없음(`skipping duplicate (idle:` 또는 감시 하나가 안 된 `(monitoring-unavailable:`)으로 끝나면 `dream_tick.py`를 떼어 내 돌린다. 하루(`JIRA_FOLLOWUP_TZ`) 한 번 `sdlc.sh dream --check`가 exit 10(due)이면 `sdlc.sh dream`이 낸 브리프로 `claude-opus-5-5` medium dreamer 하나(label `role=dreamer`, 최소 환경)를 띄우고, dreamer는 `sdlc.sh dream --done --through <close>`로 끝낸다. `dream` 명령이 없는 옛 kit, 실행 중인 dreamer가 있으면 건너뛴다. 확인·브리프·띄우기 실패와 Paseo 조회 실패는 하루를 소비하지 않아 다음 idle tick이 다시 시도한다(하루 3회까지, 띄우기 시간 초과 뒤 실행 중 dreamer가 보이면 launched). 결과는 `cat "$JIRA_FOLLOWUP_HOME/dream-last.json"`, 출력은 `dream.log`, 띄움·오류는 일지 `dream-` 줄이다. 같은 날 다시 확인하려면 `dream-last.json`의 `day`를 지운다. 끄기: `echo off > "$JIRA_FOLLOWUP_HOME/dream"`.
- 메모리에 무엇을 남기는지와 `close` 전 `Memory:` 줄은 RUNBOOK §0-1이 정본이다.

변경 이력(병렬 티켓 오케스트레이터, 결정 화면, 스테이징 승인 흐름 등)은 [`docs/history.md`](docs/history.md)에 있다. 현재 규칙의 정본은 RUNBOOK이다.

## 새 PC 설치

1. 설정: `mkdir -p ~/.jira-followup && cp config.example.env ~/.jira-followup/config.env` 후 값을 채운다(다른 위치는 `JIRA_FOLLOWUP_HOME`). 비밀값은 넣지 않는다. Jira 토큰은 `JIRA_TOKEN_FILE`이 가리키는 파일이나 환경변수에 둔다.
2. 심링크: 위 표의 `~/.local/bin` 경로를 이 폴더 스크립트로 `ln -sfn` 한다. `acli` 인증, `jk` context(dev·stg), sdlc-kit(`$JIRA_FOLLOWUP_WORKSPACE/.sdlc/config.md`의 `kit:`, `human_gates:` 비움), 실행기 `paseo`(또는 `PASEO_BIN`)가 필요하다.
3. 결정 화면: `echo app > "$JIRA_FOLLOWUP_HOME/decision-surface"`(app·tui·web은 모두 `~/.sdlc/todo.md`를 보여 주는 같은 할 일 목록, html은 로컬 브라우저 결정 페이지). 할 일 화면은 sdlc-kit to-do(`node <kit>/todo/todo.mjs`, v2.15 이상, 고정·버튼·작업 중 표시는 v2.26 이상)와 node가 필요하다. 바꾸면 다음 결정부터 적용되고, 이미 연 결정은 연 화면에서 마저 받는다.
4. 스케줄: Paseo 스케줄이나 cron 등으로 `jira-daily-wake`를 부른다. 예: cron `0,30 8-18 * * 1-5`, 시간대 `Asia/Seoul`, cwd는 `$JIRA_FOLLOWUP_WORKSPACE`. 깨우기 모델을 쓰는 스케줄러면 싼 모델(예: `gpt-6-luna` medium)에 "`jira-daily-wake`만 실행(timeout 600000 ms 이상)하고 마지막 줄로 답"만 시킨다. 주간 회고는 `RETRO.md`를 따르는 별도 스케줄로 둔다.

## 함정

규칙만 둔다. 언제·어디서 겪었는지는 [`docs/history.md`](docs/history.md)에 있다.

- JQL로는 ADF 멘션을 찾을 수 없다(`comment ~`·`watcher` 안 됨). 스캐너는 `updated >=`로 받아 댓글의 mention `attrs.id`로 거른다.
- `acli workitem search --fields key`만 주면 결과가 모두 null이다. `key,summary`를 준다.
- 아침 §5에서 `state.json`을 저장할 때 `followup` 키를 보존한다. 덮어쓰면 이미 답한 댓글에 다시 답한다.
- `jk`는 JSON을 출력한 뒤 약 60초를 더 기다렸다가 끝난다. 검증기의 jk timeout은 120~150초다.
- dev와 스테이징 DB가 같은 VIP를 쓰면 스테이징 설정으로 조회해도 VPN이 dev면 dev가 답한다. `preflight_env.py`가 `@@hostname` 접두어(`DB_HOST_PREFIX_*`)로 잡아 `env-human`으로 돌린다.
- luna 분류는 한 건씩, 수신자 맥락(`mentionsMe`·`assignedToMe`)과 함께 보낸다. `enforce`는 섀도 데이터가 쌓인 뒤 운영자 승인으로만 켠다.
- 잠금 해제는 `jira-daily-guard --release`로 한다. 직접 `rm -f`는 자동 승인 검토가 거부한다.
- 아침 fetch 대상은 `REPO_GLOB`에 맞고 `origin` 원격이 있는 저장소뿐이다.
- 비용·토큰 절감은 실측 원본 없이 추정하지 않는다(tick 시간은 가드 `timings`로 본다).
- worktree의 `.git`은 폴더가 아니라 파일이다. 이름으로 `.git`을 빼는 탐색은 worktree 안으로 들어가므로 `.git` 파일·폴더가 있는 곳을 건너뛴다. `.scratch`가 커지면 `scratch_cleanup.py`로 줄인다.
- 채팅 CLI의 작업 공간이 사라지면 `TASK_SPACE_NOT_FOUND`가 나온다. `teams_room`이 `connect`(같은 프로필에 새 공간) 후 한 번 다시 읽는다.
- `runs/`에는 실행 토큰 폴더만 둔다(아침 점검 증거는 `morning/`). 토큰 모양이 아니고 실행 표식도 없는 항목은 가드·잠금이 무시하고, `run_lock.py archive`는 `non_token=<n>`으로, `scratch_cleanup.py --runs`는 이름·크기로 보고한다. 지우기는 사람이 정한다.
- 공장 셸에서 here-document·here-string으로 임시 파일을 만들지 않는다(파일 쓰기 도구를 쓴다). 대기는 `sleep` 루프 대신 `wait-for`다.
