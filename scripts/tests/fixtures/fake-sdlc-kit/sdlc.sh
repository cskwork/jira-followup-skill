#!/usr/bin/env bash
# 시험용 가짜 sdlc.sh(test_lifecycle.py, test_dream_tick.py). 실제 sdlc-kit·작업 루트의 .sdlc를 읽지 않는다. 동작은 fake_sdlc.py.
[ -z "${FAKE_SDLC_LOG:-}" ] || printf '%s\n' "$*" >> "$FAKE_SDLC_LOG"
exec python3 "$(dirname "$0")/fake_sdlc.py" "$@"
