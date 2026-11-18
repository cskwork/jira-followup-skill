# Jira follow-up 오케스트레이터 참조 (상황별로 절만 읽는다)

[`ORCHESTRATOR.md`](ORCHESTRATOR.md)(매 tick 핵심)이 링크한 상황이 생겼을 때만 그 절을 읽는다: `python3 $S/runbook_section.py "$S/../ORCHESTRATOR.md" <절>`(두 파일을 함께 찾는다). 절 번호는 핵심과 하나의 체계이며 겹치지 않는다. 변수 `D`·`T`·`S`는 ORCHESTRATOR §1-1과 같다.

### 0-1. 대상 범위

담당자가 운영자인 티켓의 새 댓글, 운영자를 @멘션한 댓글, 담당자가 운영자인 프로젝트(`JIRA_PROJECT`) 시작 전(백로그·해야 할 일, `[보류]` 제외) 티켓의 백로그 접수(`followup_scan.py` 조건), 그리고 채팅 방(`TEAMS_ROOM_CHAT_ID`)을 설정했다면 그 방에서 다른 사람이 운영자를 부른 메시지(감시 도구 조건, RUNBOOK §7-8). 범위 밖 티켓은 운영자가 `--key`로 직접 지정했을 때만 다룬다.

### 1-2. 이전 실행·임대 회수

`guard-last.json`의 `reconcile:<이전 토큰>`, `runs/*/lost`, 가드 sweep이 회수한 티켓 임대(`runs/<tt>/lost`, 일지 `lost-lease: <KEY> <tt>`)는 티켓을 고르기 전에 이 순서로 처리한다. 결과 파일이 없는 이전 토큰 작업자도 회수 대상이다. 다음 임대는 큐 `stage`·`delivery`와 sdlc-kit `.state`에서 이어가므로 회수는 큐를 실제 검증 결과에 맞추는 데까지만 한다.

1. `python3 $S/run_lock.py recover --dir "$D" --token <이전 토큰> --current-token "$T"`로 이전 작업자의 마지막 출력을 `runs/<이전 토큰>/<id>.recovered.txt`에 저장한다. 보관된 작업자도 `paseo agent logs <id>`로 읽을 수 있다. tee 결과가 비었거나 없으면 로그로 보완한다.
2. `runs/<이전 토큰>/*.json`·회수 로그와 해당 키의 `.sdlc/work/<slug>/logs/` 검증 JSON을 대조한다. `status=done`만으로 단계를 올리지 않는다. 실제 검증 결과에 맞춰 큐를 고치고 근거를 일지에 남긴다. 큐 쓰기 전 소유권을 다시 확인한다.
3. 대조가 끝난 뒤에만 `python3 $S/run_lock.py reconciled --dir "$D" --token <이전 토큰> --current-token "$T"`을 실행한다. 이 명령은 토큰의 running 에이전트가 없음을 다시 확인하고 `ended`를 쓴다. running이 남아 있거나 조회 실패면 `lost`를 유지하고 다음 단위로 넘긴다.

이전 토큰의 보관되지 않은 `key=<KEY>` 작업자가 있고 `ended`가 없으면 그 키는 고르지 않는다(ORCHESTRATOR §1-1 `blocked`). 잠금·생존 판정의 정본은 RUNBOOK §7-0이다.

### 1-3. 참고 입력

- `triage-shadow.jsonl`: 가드가 `gpt-6-luna`로 댓글을 한 건씩 분류한 섀도 판정이다(`triage-mode` 기본 `shadow`, `followup_triage.py`). 참고만 한다. 이 판정으로 이벤트를 건너뛰지 않는다.
- 티켓 임대의 tt·agentId 정본은 임대 파일(`tickets/<KEY>.json`, `run_lock.py lease-list`)이다.
- `followup-queue.json`·`approvals.json`·`state.json` 전체는 가드·스크립트가 읽는다. 오케스트레이터는 키 단위로 `ticket_packet.py`를 쓰고, 전체 값이 꼭 필요할 때만 packet `links`의 파일을 `jq`로 그 키만 읽는다.

### 1-4. 일시정지 처리

정본은 RUNBOOK §7-H다. `guard-last.json`의 `hold`(설정)·`held`(키별 사유)와 `hold:<KEY>(사유)` reason이 있으면 막힌 키의 이벤트·runnable·승인은 고르지 않고 보고에 `hold` 줄로만 올린다. `lease-acquire`의 `{"reason":"hold"}`는 다음 후보로 넘어가고, `jira-worker` exit 3의 `hold:` stderr는 재시도하지 않는다. 큐 `waiting`·`handled`를 hold 때문에 바꾸지 않는다. 판정이 필요하면 `python3 $S/factory_hold.py check --dir "$D" --key <KEY>`(0 진행, 3 막힘).

### 2-1. 순차 runtime drain (`lanes=0`)

`lanes=0`(되돌리기 경로)일 때만 쓴다. 남아 있는 티켓 임대는 끝날 때까지 두고, 그 키와 충돌 키는 고르지 않는다(rank가 뺀다). 순서는 회수(ORCHESTRATOR §1-1) → summary 초기화 → 아침 점검 → 이벤트 처리 → 아래 drain → 보고다.

마감은 **`running.json.startedAt + 165분`**이며 오케스트레이터를 띄운 시각이나 현재 시각으로 다시 잡지 않는다. `drain` 파일이 없거나 값이 1 이상이면 켜지고, `0`이면 티켓 1건 동작이다.

1. 아침 점검·이벤트 작업자를 시작하기 전에 소유권 확인 뒤 `summary-init`(ORCHESTRATOR §2-0의 1과 같다)을 실행한다. 시작한 runtime 단위 수는 0부터 센다.
2. **첫 단위 뒤의 모든 추가 runtime 단위 직전** `followup_scan.py`와 (채팅 방을 설정했다면) `python3 "$S/teams_room.py" scan --dir "$D"`(또는 `FOLLOWUP_IDLE_TEAMS_CMD`)를 다시 실행한다(기존 파일이 10분 이내여도 생략하지 않는다. 채팅 읽기 실패는 이 단위를 막지 않고 보고에만 남긴다). 새 이벤트가 있으면 §7-2 작업자를 순차로 끝낸 뒤 후보를 다시 읽는다. 미사용 stg 승인·pending focus·focus·다른 항목은 한 항목의 backoff와 무관하게 확인한다.
3. `jira-daily-guard --dir "$D" --owns "$T"` exit 0을 확인한다. `python3 $S/drain.py --dir "$D" --token "$T" next-budget --default-timeout 120m --units <시작한 runtime 수>`를 실행한다. `canStart=false`면 선정하지 않고 6으로 간다. `timeout`을 작업자 timeout으로 쓴다. 계산은 `min(기본 timeout, deadline − now − 10분)`, 시작 조건은 `deadline − now ≥ 30분`이다. `drain=0`이면 첫 티켓 이후 추가 단위도 시작하지 않는다.
4. 최신 큐·승인·pending 줄에서 실행 가능한 후보를 판정한다(ORCHESTRATOR §1-1 키 차단·waiting·환경·항목별 backoff 준수). `python3 $S/drain.py --dir "$D" --token "$T" rank --focus <있을 때만 KEY> --eligible <실행 가능 KEY들>`의 순서를 따른다: **미사용 stg 승인 → pending focus → focus → `waiting=null`의 `createdAt` 오름차순 → `handled[첫 commentId].at` → 키 번호**. `createdAt`이 없는 기존 항목은 다음 큐 쓰기 때 채운다. 백필 시각은 이번 쓰기 시각이며, 백필 전 선정은 handled 시각으로 동률을 가른다. 같은 키를 다시 고를 때도 증거 있는 다음 단계가 있어야 한다.
5. 선정 직전 키 차단과 소유권을 다시 확인하고 `jira-worker` 한 단위를 실행한다. 끝나면 ORCHESTRATOR §4 검증과 §2-2 큐 검증을 한다. `summary-record --key <KEY> --unit <단위> --started-at <epoch> --ended-at <epoch> --workers <실제 작업자 수> --runtime --result <작업자 결과 JSON>`으로 기록한다(모두 `drain.py --dir "$D" --token "$T"` 뒤 인자다). runtime 수를 늘리고 2로 돌아간다. 이벤트·검수 등 비-runtime 작업자도 `--runtime` 없이 기록해 `workers`에 빠짐없이 합산한다.
6. 마감·순서 때문에 실행하지 못한 runnable 항목마다 소유권 확인 후 `drain.py --dir "$D" --token "$T" defer --key <KEY> --reason <budget|priority|실제 사유>`로 `deferredAt`·`deferReason`·`deferCount`를 남긴다. 아무 작업자도 실행하지 않았으면 summary의 `workers=0`을 그대로 남긴다. 면제를 만들려고 작업자 수를 올리거나 실제 막힘을 단순 미룸으로 바꾸지 않는다. 보고 후 잠금을 해제한다.

직전 실행 `runs/<token>/summary.json`의 `workers ≥ 1`이고 `deferredAt`이 직전 가드 이후인 항목만 다음 가드의 stuck 면제 대상이다. `deferCount ≥ 4`이면 면제 없이 stuck + 알림이다. `deferCount`는 처리 또는 내용 해시 변경 시 0이다. 해시는 `deferredAt`·`deferReason`·`deferCount`·`updatedAt`·`createdAt`·`backoffUntil`·`backoffLevel`을 제외한다.

### 2-3. 브랜치 판정·병합 충돌·env 재판정

- 티켓 작업 전에 `python3 $S/ticket_refs.py <KEY>`를 돌린다(읽기 전용). `carried`면 먼저 `opus` 검수자에게 브랜치 판정을 맡긴다. 검수자는 커밋 diff와 운반 브랜치의 다른 티켓 커밋을 비교해 셋 중 하나를 고르고 근거를 JSON `summary`에 쓴다(`stage`에 판정값).
  - `split`: `[KEY]` 커밋이 다른 티켓 코드에 기대지 않는다. → `sol`이 새 `fix/<KEY>`를 만든다(push 전 정지). → `opus` 검수자가 `range-diff`·`check_base`·테스트 결과를 보고 승인해야 push한다.
  - `carry`: `[KEY]` 커밋이 운반 브랜치의 다른 티켓 기능을 고치거나 그 위에 쌓였다(예: 다른 티켓의 QA 지적을 고친 커밋). → 운반 브랜치로 진행하고 함께 올라가는 키를 기록한다.
  - `human`: 판단 근거가 부족하거나 분리 시 충돌이 난다. → `waiting=human-risk`.
- 병합 충돌: 해결 작업자가 `merge-resolution-<repo>.md`와 테스트 결과를 남기면, 해결에 참여하지 않은 새 `opus` 작업자가 검수한다. 검수자는 기록·`git diff origin/<target>`·테스트 출력을 보고 `stage`에 `approved` 또는 `rejected`를 쓴다(단위 이름표다. `delivery`는 바꾸지 않는다). `approved`면 다음 단계로 간다(dev push, 또는 stg 준비 완료). `rejected`면 같은 해결을 한 번만 다시 맡기고, 두 번째도 거절되면 `waiting=human-risk`로 둔다.
- 큐에 `waiting=human-risk`이고 note의 사유가 병합 충돌뿐이면(충돌이면 멈추던 예전 규칙으로 멈춘 것 포함) 이번 실행에서 충돌 해결 단위로 재개한다.
- env 대기를 다시 볼 때 RUNBOOK §7-3-0의 `nextStep`·`needs`부터 계산해 큐에 저장한다(`preflight_env.py --queue-item`). 빈 `needs`면 env 대기만 풀어 선정 대상에 넣고, `needs`가 있으면 그 값으로 preflight를 돌려 exit 0일 때만 푼다. 원인·다음 동작이 불명확한 기존 `stage=intent` 항목은 재판정한다.

## 3. brief

파일은 `<state_dir>/briefs/YYYYMMDD-HHMM-<KEY>-<단위>.md`다. 짧게 쓰고, 그 단위에 필요한 것만 넣는다. 작업자는 RUNBOOK §0(권한·기록·사람 대기·자동 절차)을 항상 받으므로 brief에 다시 쓰지 않는다. RUNBOOK §7-6은 인용해도 작업자에게 전달되지 않는다(오케스트레이터 전용).

아침 점검 brief는 `scripts/morning_check.py --dry-run` 수집 → RUNBOOK §1의 `judgments.json` 작성 → `--execute --judgments <파일>`만 맡긴다. 날짜별 보조 스크립트를 만들지 않는다. 전이·보고·state·노트 보드·완료 표식은 고정 스크립트가 처리하고, 결과 마지막 줄과 `summary.json`을 받는다.

버그는 역할별 기대 화면·동작과 티켓 또는 기획 정본(`<기획 정본 저장소>`, 있으면) 근거를 넣는다. 정본에 없는 수정 방향은 확정하지 않는다. 기대 동작 미정은 RUNBOOK §7-2 (나)로 남기고, (가)가 있으면 재현만 맡긴다. 담당·브랜치 choice 결정 뒤에도 명확도 ≥90%를 다시 확인한다.

1. 목표 한 문장.
2. 따를 RUNBOOK 절 번호. 절 내용을 다시 적지 않는다.
3. 대상: KEY, commentIds, slug·stage·service·branch(큐에 있으면).
4. 끝 조건: 작업자 실행 전 KEY·stage·필수 검증·환경을 호출자가 `requirements.json`으로 선언한다([계약](docs/retrospective-runtime.md), 이벤트 단위는 RUNBOOK §0-1 `event`). 작업자 자체 면제는 금지다. 어떤 검증 JSON을 어디에 저장하면 done인지 쓴다. 티켓 작업이면 `.sdlc/work/<slug>/logs/check-base.json`·`check-deploy.json`, Jira 댓글이면 `check_adf.py` 되읽기 결과다. environment에는 검증기가 실제로 출력하는 필드만 쓴다(`check_deploy.py`는 `job`·`build`·`context`와 `--key`를 줄 때의 `key`). 없는 필드는 `verifier-field-missing:<필드>`로 실패한다.
5. 이번 단위에서 하지 않을 쓰기(예: 이벤트 처리 brief에는 "코드 수정·배포 금지").
6. 백로그 접수 티켓의 `진행 중` 이동은 RUNBOOK §3 예외대로 `jira_status_check.py <KEY> start`의 `STEP`을 한 단계씩 따르게 쓴다. Jira 상태 이동을 맡기면 순서를 RUNBOOK §7-3-5대로 쓴다: E2E PASS → 결과 댓글 게시·`check_adf.py` PASS → `jira_status_check.py <KEY> <dev|staging> --evidence-comment <댓글 ID>` → `MOVE`·`STEP`일 때만 이동·되읽기. 댓글 앞에 이동을 두지 않는다.

실행: `~/.local/bin/jira-worker <sol|opus> "<KEY 단위>" <brief 경로> [timeout] --dir <state_dir> --token <run_token> --key <KEY> --requirements <호출자가 쓴 requirements.json> [--resources <자원,…>]`. 자원 이름과 exit 4는 RUNBOOK §7-3-L이다. 결과는 `worker-result.schema.json` 모양의 JSON이다. 호출 때 stderr도 `2> <state_dir>/runs/<run_token>/<KEY>-<unit>.stderr`로 저장한다. 티켓 작업 기본 timeout은 `120m`이며 실제 값은 `drain.py next-budget`의 `timeout`이다. 아침 점검처럼 키·전진할 stage가 없는 단위는 worker와 summary-record의 `--key`·`--requirements`를 생략한다. 선언한 stage를 전진하는 티켓 단위는 requirements를 생략할 수 없다. 작업자는 부모 없이 실행되고 결과를 stdout과 `runs/<run_token>/<KEY>-<unit>.json`에 함께 쓴다. 시도별 checkpoint·실행 영수증은 `attempt-*`에 보존하고 `<결과>.worker.json`이 마지막 영수증을 가리킨다. nonzero·timeout의 checkpoint는 복구 증적이며 실행 성공으로 간주하지 않는다. exit 3이면 소유권·키 차단을 다시 확인하고 재시도하지 않는다.

## 4. 결과 확인 (판정은 스크립트)

- 먼저 `python3 $S/drain.py --dir "$D" --token "$T" check-result --key <KEY> --result <결과 JSON> --stderr <저장한 stderr> --requirements <실행 전에 선언한 requirements.json> --worker-metadata <결과 JSON>.worker.json`를 실행한다. 결과·stderr의 `rate limit`·`rate-limit`·`429`·`overloaded`는 `failureType=infra`, `retryAllowed=false`이고 `deferReason=rate-limit`과 항목별 backoff(30분 × 2^level, 최대 2시간)를 기록한다. 이 실패는 **재시도하지 않는다**(같은 결과에 `rate-limit`을 별도로 중복 호출하지도 않는다). 만료 전에는 해당 항목만 runnable·stuck에서 제외하고, 다른 항목·새 이벤트·pending focus는 계속 처리한다. 키 없는 아침 단위의 요금 제한은 infra 교훈만 남기며 큐 항목을 만들지 않는다.
- 선언한 stage는 `check-result` exit 0과 `stageComplete=true` 전에 전진하거나 `processed` 처리하지 않는다. 필수 check 누락·FAIL·SKIP·중복·KEY/stage/환경 불일치·증적 파일 없음은 exit 4, `retryAllowed=false`다. 통과한 증적과 결과는 gate sidecar에 보존한다. 작업자가 `status=done`이라고 해도 이 판정을 면제하지 않는다. 큐의 `waiting`은 출력의 `queueWaiting`으로 두고 note에 이유를 쓴다. `env-human`·`env-down`이면 RUNBOOK §7-3-0대로 막힌 다음 동작의 `nextStep`(DB면 `profile`도)을 정해 `preflight_env.py --queue-item`의 `item`을 저장한다(가드가 `needs`를 점검해 VPN 연결을 알리고, 연결되면 `waiting=null`로 재개). 그 밖은 `human-risk`다. 작업자 결과가 `waiting=human-risk`면 질문·권장 답 또는 `humanAction`을 `patch ... --from-result <결과>`로 옮긴다(RUNBOOK §0-2).
- 성공 검증 뒤에는 `drain.py --dir "$D" --token <토큰> processed --key <KEY>`로 처리 표시·`deferCount` 초기화와 backoff 삭제를 한다.
- 의심되면 같은 검증 스크립트를 읽기 전용으로 다시 돌린다. `check_base.py`, `check_deploy.py`(jk가 약 60초 걸린다), `check_adf.py`.
- `jira-worker` exit 2(stderr `jira-worker: brief:`·`requirements:`·`resources:`)는 작업자 실패가 아니다. 그 선언을 고쳐 다시 실행하고 재시도 횟수에 넣지 않는다.
- `retryAllowed=false`인 검증 실패는 같은 작업자를 자동 재시도하지 않는다. 그 밖의 작업자 실패(`status=failed` 또는 종료 코드 ≠ 0)는 같은 단위를 같은 작업자에게 **한 번만** 다시 맡긴다. 재시도도 소유권·마감 예산을 확인한다. 두 번째도 실패하면 `waiting=human-risk`(RUNBOOK §0-2 형식)로 두고 알린다. 다른 모델로 바꾸지 않는다. 예외: `check-result`가 `failureType=model-refusal`·`retryWorker=opus`를 내면(sol 작업자의 사이버보안 필터 거절) 같은 brief·requirements로 `opus`에게 한 번만 다시 맡긴다. 이것이 그 단위의 한 번 재시도이며 opus도 실패하면 `waiting=human-risk`다.
- 작업자 결과 `artifacts`의 `cleanup:<worktree 경로>`는 `check-result` 뒤 `python3 $S/scratch_cleanup.py --execute --result <결과 JSON>`으로 지운다(정본 RUNBOOK §0 git 줄). 남긴 것(`kept`)·실패(exit 1)는 일지에 한 줄로 남기고 단위 판정은 바꾸지 않는다.

## 결정·측정

### 5-1. 결정 spec

모두 `decision-request`로 연다(ORCHESTRATOR §5). 담당·브랜치의 choice 결정은 명확도 ≥90% 게이트를 면제하지 않는다. `related` 후보가 있으면 착수 전에 결정을 열고, 결정 범위 밖의 기대 동작·정책은 미정으로 남긴다. 결정 후에도 RUNBOOK §7-2 재현·질문 판정을 다시 거친다. judge 모드가 켜져 있으면 `decision-request`가 위험이 낮은 choice를 판정 에이전트에게 분리 실행으로 넘기고 바로 `decision-request: judge reviewing <name>`을 출력한다. judge가 정하면 `decisions.jsonl`(by=judge) 기록 뒤 `jira-daily-wake --now --key`로 재개되고, 넘기면 같은 결정 코드로 사람 항목이 열린다(RUNBOOK §7-6 판정 에이전트).

- stg 승인: `stg-approval-request <KEY>`(스테이징 DB 마이그레이션 검사 exit 0일 때만 결정 생성) (`waiting=stg-approval`이 새로 생길 때). 할 일 화면은 열린 스테이징 승인 묶음 하나에 키를 추가한다. `승인`은 전부, `승인 101,102`·`보류 102`는 해당 키만 처리한다(RUNBOOK §7-6).
- dev/staging DB 변경(마이그레이션): 승격 범위에 `MIGRATION_GLOB`(기본 `docs/db/migration/*.sql`)처럼 실행이 필요한 SQL이 있으면, 대상 DB에서 읽기 전용으로 적용 여부를 먼저 확인한다. 미적용이면 `{"kind":"sql-run","key","title","summary","config":"app_dev|app_stg","expect_host":"dev-db|stg-db","file":"<SQL 파일>","guard_sql":"<적용 전이면 기대값이 나오는 SELECT>","guard_expect":0}` spec을 `<state_dir>/decision-pages/`에 쓰고 `decision-request spec <spec>`. 실행 결과 JSON이 `decisions/`에 남는다. 운영(prd) DB는 결정으로도 실행하지 않는다. 운영 실행 SQL은 Jira 댓글에 코드 블록으로 남긴다.
- 채팅 답장(선택): 작업자가 `teams_room.py draft`로 만든 `teams-reply` 결정이다(RUNBOOK §7-8). 승인 = `teams_room.py send`(또는 `CHAT_SEND_CMD`)로 한 번 발송·verify, 보류 = 발송 없음.
- 그 밖의 판단(요청자가 운영자 본인이라 게시하지 못한 질문, 의미 충돌, 분리 불가, 초안 게시 여부): `{"kind":"choice","humanReason":"product-scope","key","title","summary","background","intent","question","options":[{"id","label","detail"}],"recommended":"<option id>","recommendReason":"<근거 한두 문장>"}` spec으로 연다. `recommended`(AI 권장 선택지)와 `humanReason`은 필수다. `humanReason`은 `product-scope`·`security-plan`·`prod-data`·`policy`·`conflict` 중 실제 사유를 쓴다. 절차 질문은 RUNBOOK §0-3 자동 규칙을 따르고, 보안 계획은 `security-plan`으로 사람 승인을 받는다.
  - 할 일 앱은 제목 전체와 질문 줄 앞 160자만 보여 준다. 그래서 `title`은 결정할 것을 키 없이 쉬운 말 한 줄로, 각 `label`은 25자 안팎의 쉬운 말로(기술 내용은 `detail`로), `recommendReason` 첫 문장은 파일 경로·코드명 없이 기획자도 읽을 수 있는 한 문장으로 쓰고 근거 경로는 괄호나 둘째 문장에 둔다.
  - 앱의 결정 요약(정해 주실 것·한 줄로 보면·지금 상황·왜 필요한가요)은 spec의 `question`, AI 권장 선택지와 이유, `background`(무슨 문제이고 지금까지 무엇을 했는지, 쉬운 말 한두 문장), `intent`(이 결정 뒤 하려는 일과 목적)에서 만든다. `background`·`intent`가 없으면 그 티켓 sdlc `intent.md`의 At a glance를 쓴다.
  - 증거로 고를 수 있는 최선을 권장하고, 근거에는 증적 경로나 정본을 짧게 적는다.
- 결정 화면은 `<state_dir>/decision-surface` 하나다(RUNBOOK §7-6). `decision-request: judge reviewing <name>`이 나오면 사람 결정을 연 것과 똑같이 기다린다.
- 결정 답 읽기: 다음 실행에서 `decisions.jsonl`의 그 키 최신 줄(`ticket_packet.py`의 `decisions`). `option=free-text`이면 `note`가 사람이 번호 대신 쓴 지시다. 선택지 중 뜻이 맞는 것이 있으면 그것으로, 없으면 그 말대로 권한 안에서 진행하고 같은 질문을 다시 열지 않는다. 뜻이 둘로 갈려 진행할 수 없을 때만 그 말을 인용해 새 질문을 연다.

### 5-2. 측정

ORCHESTRATOR §5의 `summary-record --result`에 더해, 재시도 전에도 실패 영수증을 먼저 기록한다. 측정 원본이 있으면 `--observations`로 전달하고 없으면 비용·토큰·사람 개입·대기·재작업은 null/unknown으로 둔다. 실제 버튼 결정은 decisions.jsonl에서 자동 집계하며 폴링은 세지 않는다([측정 계약](docs/retrospective-runtime.md)).
