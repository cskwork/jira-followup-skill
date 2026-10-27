#!/usr/bin/env python3
"""공유 상태를 잠금 안에서 읽고 키 단위로 갱신한다."""

import argparse
from collections import Counter
from contextlib import contextmanager
from datetime import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
import followup_config as cfg  # noqa: E402


BOOKKEEPING = {"deferredAt", "deferReason", "deferCount", "updatedAt", "createdAt",
               "backoffUntil", "backoffLevel"}


def item_digest(item):
    content = {key: value for key, value in item.items() if key not in BOOKKEEPING}
    return hashlib.sha256(json.dumps(content, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode()).hexdigest()


@contextmanager
def locked(directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / ".state.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def atomic_text(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix="." + path.name + "-", suffix=".tmp", delete=False) as out:
            temporary = Path(out.name)
            out.write(text)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def atomic_json(path, value):
    atomic_text(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def read_object(path, optional=True):
    path = Path(path)
    if optional and not path.exists():
        return {}
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("expected object: " + str(path))
    return value


def _target(directory, filename):
    directory = Path(directory).resolve()
    path = (directory / filename).resolve()
    if not path.is_relative_to(directory) or path == directory:
        raise ValueError("state filename must stay within directory")
    return path


def transaction(directory, filename, callback):
    """최신 객체에 callback을 적용한다. callback 실패 시 원본을 유지한다."""
    target = _target(directory, filename)
    with locked(directory):
        data = read_object(target)
        before = json.loads(json.dumps(data))
        callback(data)
        if target.name == "followup-queue.json":
            for key, row in data.items():
                previous = before.get(key)
                if isinstance(row, dict) and row != previous:
                    if not row.get("createdAt"):
                        row["createdAt"] = datetime.now(ZoneInfo("Asia/Seoul")).isoformat()
                    # 대기 중 바뀐 항목도 재개할 때 이전 미룸 횟수를 물려받지 않는다.
                    if (isinstance(previous, dict) and "deferCount" in row
                            and item_digest(row) != item_digest(previous)):
                        row["deferCount"] = 0
        atomic_json(target, data)
        return data


def patch(directory, filename, path, values, delete=()):
    """path 아래 키만 합친다. 다른 항목과 형제 키는 보존한다."""
    if (not isinstance(path, list) or not all(isinstance(key, str) for key in path)
            or not isinstance(values, dict) or not isinstance(delete, (list, tuple))
            or not all(isinstance(key, str) for key in delete)):
        raise ValueError("path/delete must contain strings and values must be an object")

    def apply(data):
        node = data
        for key in path:
            node = node.setdefault(key, {})
            if not isinstance(node, dict):
                raise ValueError("patch path must contain objects")
        node.update(values)
        for key in delete:
            node.pop(key, None)

    return transaction(directory, filename, apply)


def append_pending(directory, line):
    """한 줄을 잠금 안에서 추가한다. consume의 교체와 경합하지 않는다."""
    if not isinstance(line, str) or not line.strip() or "\n" in line or "\r" in line:
        raise ValueError("pending focus must be one nonempty line")
    with locked(directory):
        target = Path(directory) / "pending-focus.jsonl"
        with target.open("a", encoding="utf-8") as out:
            out.write(line + "\n")
            out.flush()
            os.fsync(out.fileno())


def consume_pending(directory, lines):
    """처리한 줄의 개수만 지워 읽은 뒤 새로 추가된 동일 줄도 보존한다."""
    if not isinstance(lines, list) or not all(isinstance(line, str) for line in lines):
        raise ValueError("consumed lines must be an array of strings")
    requested = Counter(line.rstrip("\r\n") for line in lines)
    with locked(directory):
        target = Path(directory) / "pending-focus.jsonl"
        current = target.read_text(encoding="utf-8").splitlines(keepends=True) if target.exists() else []
        kept = []
        for line in current:
            key = line.rstrip("\r\n")
            if requested[key]:
                requested[key] -= 1
            else:
                kept.append(line)
        atomic_text(target, "".join(kept))
        return len(current) - len(kept)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dir", type=Path, default=Path(os.environ.get("JIRA_WAKE_DIR") or cfg.HOME_DIR))
    commands = parser.add_subparsers(dest="command", required=True)
    change = commands.add_parser("patch")
    change.add_argument("filename")
    change.add_argument("--path", type=json.loads, default=[])
    change.add_argument("--json", type=json.loads, required=True, dest="values")
    change.add_argument("--delete", type=json.loads, default=[])
    append = commands.add_parser("append-pending")
    content = append.add_mutually_exclusive_group(required=True)
    content.add_argument("--line")
    content.add_argument("--json", type=json.loads, dest="row")
    consume = commands.add_parser("consume-pending")
    consume.add_argument("--lines", type=json.loads, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "patch":
            result = patch(args.dir, args.filename, args.path, args.values, args.delete)
            print(json.dumps(result, ensure_ascii=False))
        elif args.command == "append-pending":
            if args.row is not None and not isinstance(args.row, dict):
                raise ValueError("pending focus JSON must be an object")
            append_pending(args.dir, args.line if args.line is not None else json.dumps(args.row, ensure_ascii=False))
        else:
            print(consume_pending(args.dir, args.lines))
    except (OSError, ValueError, TypeError) as error:
        print("state-update: " + str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
