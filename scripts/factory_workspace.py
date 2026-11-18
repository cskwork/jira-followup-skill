#!/usr/bin/env python3
"""팩토리 에이전트가 함께 쓰는 Paseo workspace ID를 출력한다.

실행마다 `paseo run --cwd`가 새 workspace(`main`)를 만들고 에이전트 보관 뒤에도 남아
사이드바에 수백 개가 쌓였다(2026-10-30, 219개 수동 보관). 고정 workspace 하나에서 띄우면
보관된 에이전트는 사라지고 진행 중인 작업만 보인다.

Usage: factory_workspace.py --dir STATE_DIR --path WORKSPACE
- JIRA_FACTORY_WORKSPACE가 있으면 그 값을 그대로 쓴다(시험·수동 지정).
- STATE_DIR/factory-workspace의 ID가 활성 목록에 있으면 그것을, 없으면 새로 만들어 저장한다.
- 실패하면 stderr에 이유를 쓰고 빈 출력으로 0을 낸다. 호출자는 --workspace 없이 예전처럼 띄운다.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

TITLE = "Jira follow-up factory"


def paseo(*args):
    out = subprocess.run([os.environ.get("PASEO_BIN") or "paseo", *args, "--json"], capture_output=True, text=True,
                         check=True, timeout=30).stdout
    return json.loads(out)


def resolve(state_dir, path):
    record = state_dir / "factory-workspace"
    saved = record.read_text().strip() if record.exists() else ""
    if saved and any(row.get("workspaceId") == saved for row in paseo("workspace", "ls")):
        return saved
    created = paseo("workspace", "create", "--isolation", "local", "--path", str(path), "--title", TITLE)
    identity = created.get("workspaceId") if isinstance(created, dict) else None
    if not isinstance(identity, str) or not identity.startswith("wks_"):
        raise ValueError("workspace create returned no workspaceId")
    record.write_text(identity + "\n")
    return identity


def lookup(state_dir, path):
    """JIRA_FACTORY_WORKSPACE가 있으면 그 값, 없으면 resolve. 조회 실패는 알리고 ''(--workspace 없이 띄운다)."""
    workspace = os.environ.get("JIRA_FACTORY_WORKSPACE", "")
    if workspace:
        return workspace
    try:
        return resolve(state_dir, path)
    except (OSError, ValueError, AttributeError, subprocess.SubprocessError) as error:
        print(f"factory_workspace: {error}; launching without --workspace", file=sys.stderr)
        return ""


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--dir", type=Path, required=True)
    parser.add_argument("--path", type=Path, required=True)
    args = parser.parse_args(argv)
    override = os.environ.get("JIRA_FACTORY_WORKSPACE")
    if override is not None:
        print(override)
        return 0
    try:
        print(resolve(args.dir, args.path))
    except (OSError, ValueError, AttributeError, subprocess.SubprocessError) as error:
        print(f"factory_workspace: {error}; launching without --workspace", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
