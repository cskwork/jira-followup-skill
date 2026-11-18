#!/usr/bin/env python3
"""무인 팩토리 Claude 에이전트의 최소 환경(min-env).

무인 실행에 쓸모없는 사용자 맥락만 빼고 안전 장치(설정 훅·blast-radius·프로젝트 CLAUDE.md·RTK)는 그대로 둔다.
설정 출처(--setting-sources)는 바꾸지 않으므로 ~/.claude/settings.json과 workspace의 .claude 훅은 그대로 실린다.
  CLAUDE_CODE_DISABLE_AUTO_MEMORY=1  자동 메모리 MEMORY.md 색인을 싣지 않는다(Claude Code 환경 변수).
  HINDSIGHT_DISABLE_HOOKS=1          Hindsight SessionStart·UserPromptSubmit·Stop 훅이 바로 끝난다(무인 세션 30초 타임아웃 원인).
  TE_MICROSKILLS=off                 tokeneconomizer SessionStart 저장소 안내문 주입만 끈다(loopguard는 유지).
judge의 claude -p는 Read·Grep·Glob만 쓰므로 도구 목록·MCP·스킬 목록도 뺀다(JUDGE_FLAGS).

끄기: <state_dir>/min-env 파일 첫 줄이 off면 예전 명령 그대로다. 없거나 그 밖의 값이면 켠다.
Usage: factory_env.py paseo-args --dir STATE_DIR   paseo run 인자(--env K=V)를 한 줄에 하나씩 출력
"""
import argparse
import os
from pathlib import Path
import sys

CLAUDE_ENV = {
    "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
    "HINDSIGHT_DISABLE_HOOKS": "1",
    "TE_MICROSKILLS": "off",
}
JUDGE_FLAGS = ["--tools", "Read,Grep,Glob", "--strict-mcp-config", "--disable-slash-commands"]


def enabled(directory):
    try:
        lines = (Path(directory) / "min-env").read_text().split()
    except FileNotFoundError:
        return True
    except OSError as error:
        print(f"factory_env: {error}; min-env stays on", file=sys.stderr)
        return True
    return not lines or lines[0].lower() != "off"


def paseo_args(directory):
    if not enabled(directory):
        return []
    return [arg for key, value in CLAUDE_ENV.items() for arg in ("--env", f"{key}={value}")]


def judge_command(base, directory):
    """judge의 claude -p 명령과 환경. 끄면 base와 현재 환경 그대로다."""
    if not enabled(directory):
        return list(base), None
    return list(base) + JUDGE_FLAGS, {**os.environ, **CLAUDE_ENV}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["paseo-args"])
    parser.add_argument("--dir", type=Path, required=True)
    args = parser.parse_args(argv)
    for arg in paseo_args(args.dir.expanduser()):
        print(arg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
