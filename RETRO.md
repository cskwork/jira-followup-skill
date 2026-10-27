# Jira follow-up 라인 주간 회고 (예: 금요일 17:00, 스케줄러)

라인이 스스로 나아지게 하는 고리다. 이 회고는 **개선안을 만들고 보고만 한다. 병합은 사람이 승인한다.** 라인이 자기 규칙·권한을 혼자 바꾸지 않게 하기 위해서다.

## 1. 입력 (읽기 전용)

- `$JIRA_FOLLOWUP_HOME/lessons.jsonl`: 지난 회고 이후 줄. 마지막 회고 시각은 `$JIRA_FOLLOWUP_HOME/retro-state.json`의 `lastRetroAt`이다(없으면 7일 전).
- 같은 기간의 `followup-YYYY-MM-DD.md` 일지와 스케줄러 실행 기록(tick 마지막 줄: `errors`, `worker_fail`, `waiting`).
- `python3 scripts/followup_triage.py --report`: luna 섀도 판정과 실제 처리의 일치·놓침 수.
- `bash <sdlc-kit 경로>/sdlc.sh status --json`(`$JIRA_FOLLOWUP_WORKSPACE`에서): 사람 대기 기능 수와 이유.
- `python3 scripts/session_digest.py --since <lastRetroAt>`: 세션별 사용자 발화·도구 오류·훅 차단을 읽는다(공장 세션은 축약). 사용자 정정, 같은 요청의 반복, 훅 오탐, 규칙 충돌을 추출하고 원 세션 증적을 일지·교훈과 대조한다.

## 2. 할 일

1. 반복되는 실패 유형을 묶는다. 한 번뿐인 일은 기록만 하고 규칙을 만들지 않는다.
2. 유형마다 가장 작은 개선을 고른다. 검증 스크립트·가드(exit 코드로 판정)를 먼저 고려하고, 문서 규칙은 그다음이다.
3. 개선안은 이 스킬의 저장소에 `retro/YYYYMMDD` 브랜치를 `origin/main`에서 만들어 커밋한다. 코드면 실패하던 사례를 재현하는 테스트를 같이 넣고 `python3 -m unittest discover -s scripts/tests`가 통과해야 한다. 브랜치는 push하되 **`main`에 병합하지 않는다.**
4. luna `enforce` 준비도: 섀도 표본 30건 이상이고 FALSE SKIP 0건이면 "켜도 됨"으로 보고한다. 아니면 남은 표본 수를 보고한다. 직접 켜지 않는다.
5. 보고서 `$JIRA_FOLLOWUP_HOME/retro-YYYY-MM-DD.md`: 요약 3줄, 유형별 표(`유형 | 횟수 | 근거 | 개선안 | 브랜치 커밋`), 운영자 결정이 필요한 항목. 끝에 `retro-state.json`의 `lastRetroAt`을 갱신한다.
6. 알림: `osascript -e 'display notification "주간 회고: 개선안 <n>건, 결정 <m>건" with title "Jira follow-up 라인"'`.

## 3. 하지 않는 것

`main` 병합, 스케줄·권한·모델 라우팅 변경, Jira·Jenkins·서비스 저장소 쓰기, 운영 데이터 접근.

마지막 줄: `retro lessons=<n> proposals=<n> branch=<retro/...|none> decisions=<n> luna_enforce=<ready|need N more>`
