# Jira follow-up 라인 주간 회고 (예: 금요일 17:00, 스케줄러)

라인이 스스로 나아지게 하는 고리다. 이 회고는 **개선안을 만들고 보고만 한다. 병합은 사람이 승인한다.** 라인이 자기 규칙·권한을 혼자 바꾸지 않게 하기 위해서다.

## 1. 입력 (읽기 전용)

- `$JIRA_FOLLOWUP_HOME/lessons.jsonl`: 지난 회고 이후 줄. 마지막 회고 시각은 `$JIRA_FOLLOWUP_HOME/retro-state.json`의 `lastRetroAt`이다(없으면 7일 전).
- 같은 기간의 `followup-YYYY-MM-DD.md` 일지와 스케줄러 실행 기록(tick 마지막 줄: `errors`, `worker_fail`, `waiting`).
- `runs/*/summary.json`의 실제 worker 영수증·단위 소요·wallElapsedSeconds·metrics(knownSubtotal/unknownUnits), guard-last.json과 followup-scan.json의 timings/attempts, teams-scan.json(채팅 감시를 설정한 경우)의 monitoring을 읽는다. 비용·토큰·대기 관찰이 없으면 unknown으로 보고하고 절감액을 추정하지 않는다. 실제 버튼 결정과 폴링을 구분한다([측정 계약](docs/retrospective-runtime.md)).
- `python3 scripts/followup_triage.py --report`: luna 섀도 판정과 실제 처리의 일치·놓침 수.
- `bash <sdlc-kit 경로>/sdlc.sh status --json`(`$JIRA_FOLLOWUP_WORKSPACE`에서): 사람 대기 기능 수와 이유.
- `python3 scripts/session_digest.py --since <lastRetroAt>`: 세션별 사용자 발화·도구 오류·훅 차단을 읽는다(공장 세션은 축약). 사용자 정정, 같은 요청의 반복, 훅 오탐, 규칙 충돌을 추출하고 원 세션 증적을 일지·교훈과 대조한다.

## 2. 할 일

1. 반복되는 실패 유형을 묶는다. 한 번뿐인 일은 기록만 하고 규칙을 만들지 않는다.
   - `scripts/lesson_classes.json`도 검토해 `other`에서 같은 유형이 반복되면 분류 추가를, 가까운 분류끼리는 통합을 제안한다.
2. 유형마다 가장 작은 개선을 고른다. 검증 스크립트·가드(exit 코드로 판정)를 먼저 고려하고, 문서 규칙은 그다음이다.
3. 개선안은 이 스킬의 저장소에 `retro/YYYYMMDD` 브랜치를 `origin/main`에서 만들어 커밋한다. 코드면 실패하던 사례를 재현하는 테스트를 같이 넣고 `python3 -m unittest discover -s scripts/tests`가 통과해야 한다. 브랜치는 push하되 **`main`에 병합하지 않는다.** 7일 넘게 병합되지 않은 `retro/*`는 아침 점검(`morning_check.py --execute`)이 `[회고 제안] 미병합 n건` 결정 하나로 사람에게 다시 올린다(병합·삭제는 사람).
4. luna `enforce` 준비도: 섀도 표본 30건 이상이고 FALSE SKIP 0건이면 "켜도 됨"으로 보고한다. 아니면 남은 표본 수를 보고한다. 직접 켜지 않는다.
5. 보고서 `$JIRA_FOLLOWUP_HOME/retro-YYYY-MM-DD.md`: 요약 3줄, 유형별 표(`유형 | 횟수 | 근거 | 개선안 | 브랜치 커밋`), 운영자 결정이 필요한 항목을 유지한다. **각 운영자 결정은 보고서에만 두지 말고** `decision-request spec <결정.json>`으로 같은 결정 화면에도 연다. spec은 `kind=choice`, 해당 `key`(공장 전체면 `FACTORY`), `title`·`question`·`summary`(횟수·날짜·증적 1~3개), `humanReason=policy` 등 허용 사유, `options`의 `id`·`label`·`detail`, `recommended=<권장 option id>`와 `recommendReason`을 넣는다. 반복 lesson의 기존 열린 `lesson-<signature>` 결정은 새로 열지 않고 보고서에 그 코드와 증적을 인용한다. 권장 선택을 반드시 표시하고 보고서에는 열린 결정 코드·spec 경로도 남긴다. 실제 화면 열기에 성공한 뒤 `retro-state.json`의 `lastRetroAt`을 갱신한다.
6. 알림: `osascript -e 'display notification "주간 회고: 개선안 <n>건, 결정 <m>건" with title "Jira follow-up 라인"'`.

## 3. 하지 않는 것

`main` 병합, 스케줄·권한·모델 라우팅 변경, Jira·Jenkins·서비스 저장소 쓰기, 운영 데이터 접근.

마지막 줄: `retro lessons=<n> proposals=<n> branch=<retro/...|none> decisions=<n> luna_enforce=<ready|need N more>`

- 2026-10-28: ① Paseo `error`를 비실행 종료 상태로 인정해 lost 회수 복구 ② 전부 막힌 pending focus는 깨우지 않고 아침 보고 ③ 빈 값·루트 `--dir`/`--token` 거부 ④ `related-search-failed`에 예외 내용 최대 300자 보존 ⑤ 채팅 감시 성공 이력 없음·24시간 초과 실패를 이유 코드와 채팅 도구 재연결 안내로 아침 조치 보고 ⑥ RTK 대입 훅 우회를 위해 오케스트레이터·작업자 변수는 `export D=... T=... S=...` 사용.
- 2026-11-02: 아침 점검(`morning_check.py --execute`)이 `origin/dev` 포함만 보고 티켓 5건을 `진행 중` → `개발 QA 요청`으로 옮김(큐는 `waiting=env-human`·`nextStep=dev-e2e`, dev QA·결과 댓글 0건). 아침 자동 이동을 `스테이징 검수 완료` 이상 + 운영 `v*` 태그로 좁히고, 워커 이동은 `jira_status_check.py --evidence-comment`(본인·이미지 1개 이상·현재 상태 진입 뒤 댓글) 없으면 `HOLD`.
