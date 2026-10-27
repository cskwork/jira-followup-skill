#!/usr/bin/env python3
"""Read local Claude/Codex JSONL into a redacted, event-time session digest.

The interval is [since, until). ISO values without an offset use local time;
reports use UTC. Sources are never modified. Only --out writes a file.
"""

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import sys
import time


FACTORY_PREFIXES = (
    "You are the Jira follow-up factory orchestrator",
    "You are the Jira follow-up tick waker",
)
HOOK = re.compile(r"PreToolUse(?:\:[^\s]+)?\s+hook error:\s*\[([^\]]+)\]:\s*", re.I)
SECRET = re.compile(
    r"\bBearer\s+[^\s\"'<>]+"
    r"|\b(?:ghp_|github_pat_|sk-|xox[a-z]-)[A-Za-z0-9_-]+"
    r"|\beyJ[A-Za-z0-9_-]*(?:\.[A-Za-z0-9_-]+){1,2}", re.I
)
REMINDERS = re.compile(r"<(system-reminder|task-notification)\b[^>]*>.*?</\1>", re.S | re.I)


def parse_time(value):
    """Accept Unix seconds or an ISO-8601 timestamp (including Z)."""
    try:
        stamp = float(value)
    except (TypeError, ValueError):
        try:
            stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
        except (ValueError, OverflowError) as exc:
            raise ValueError("time must be ISO-8601 or Unix epoch seconds") from exc
    if not math.isfinite(stamp):
        raise ValueError("time must be a finite ISO-8601 or Unix epoch value")
    try:
        datetime.fromtimestamp(stamp, timezone.utc)
    except (ValueError, OverflowError, OSError) as exc:
        raise ValueError("time is outside the supported ISO-8601 range") from exc
    return stamp


def preview(value, limit):
    """Redact the entire value before flattening or truncating it."""
    clean = SECRET.sub("[REDACTED]", str(value))
    return " ".join(clean.split())[:limit]


def utc(stamp):
    return datetime.fromtimestamp(stamp, timezone.utc).isoformat().replace("+00:00", "Z")


def text_content(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(text_content(block.get("text", "")) for block in content
                        if isinstance(block, dict) and block.get("type") in ("text", "input_text", "output_text"))
    return ""


def user_text(content):
    text = REMINDERS.sub("", text_content(content)).strip()
    # Also exclude a notification whose closing tag was never written.
    if re.match(r"^<(?:system-reminder|task-notification)\b", text, re.I):
        return "", False
    interrupted = bool(re.search(r"Request interrupted|<turn_aborted\b", text, re.I))
    return ("" if interrupted else text), interrupted


def codex_user_content(content):
    # Codex records harness-injected instructions/environment with role=user.
    # They are context records, preceding the first actual user prompt.
    if isinstance(content, list):
        content = [block for block in content if not (
            isinstance(block, dict) and str(block.get("text", "")).lstrip().startswith(
                ("# AGENTS.md instructions", "<environment_context>")))]
    return content


def output_text(value):
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(output_text(block) for block in value)
    if isinstance(value, dict):
        if "text" in value:
            return output_text(value["text"])
        return json.dumps(value, ensure_ascii=False)
    return ""


def failed_output(value):
    if isinstance(value, list):
        return any(failed_output(block) for block in value)
    if isinstance(value, dict):
        return bool(value.get("is_error") or value.get("error")
                    or value.get("exit_code") not in (None, 0, "0")
                    or ("text" in value and failed_output(value["text"])))
    text = output_text(value)
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        parsed = None
    return ((isinstance(parsed, (dict, list)) and failed_output(parsed))
            or bool(HOOK.search(text))
            or bool(re.search(r"(?:Process exited with code|exit_code\s*[\":=]+)\s*-?[1-9]\d*", text))
            or text.lstrip().startswith(("Error:", "Error executing tool", "Tool execution failed")))


def command_input(value):
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except ValueError:
            return value
        return command_input(parsed)
    if isinstance(value, dict):
        command = value.get("command", value.get("cmd", ""))
        return " ".join(map(str, command)) if isinstance(command, list) else str(command)
    return ""


def source_files(home):
    paths = [("claude", path) for path in (home / ".claude/projects").glob("*/*.jsonl")
             if not path.name.startswith("agent-")]
    paths.extend(("codex", path) for pattern in ("sessions/*/*/*/*.jsonl", "archived_sessions/*.jsonl")
                 for path in (home / ".codex").glob(pattern))
    return sorted(paths, key=lambda pair: str(pair[1]))


def read_sessions(home, warnings):
    sessions = {}
    for provider, path in source_files(home):
        rows = []
        session_id = path.stem
        subagent = False
        try:
            with path.open(encoding="utf-8", errors="replace") as stream:
                for line in stream:
                    if not line.strip():
                        continue
                    try:
                        row = json.loads(line)
                    except ValueError:
                        warnings["malformed_lines"] += 1
                        continue
                    if not isinstance(row, dict):
                        warnings["malformed_lines"] += 1
                        continue
                    if provider == "claude":
                        session_id = row.get("sessionId") or session_id
                    elif row.get("type") == "session_meta":
                        meta = row.get("payload") or {}
                        session_id = meta.get("id") or meta.get("session_id") or session_id
                        subagent = (isinstance(meta.get("source"), dict) and "subagent" in meta["source"])
                        subagent = subagent or meta.get("source") == "subagent"
                    rows.append(row)
        except OSError:
            warnings["unreadable_files"] += 1
            continue
        if subagent:
            continue
        key = (provider, str(session_id))
        session = sessions.setdefault(key, {"rows": [], "seen": Counter()})
        # Occurrence counts preserve identical repeated turns in a single log,
        # while merging overlapping live/archive copies of the same session.
        occurrences = Counter()
        for row in rows:
            fingerprint = hashlib.sha256(json.dumps(row, sort_keys=True).encode()).digest()
            occurrences[fingerprint] += 1
            if occurrences[fingerprint] > session["seen"][fingerprint]:
                session["rows"].append(row)
        session["seen"] |= occurrences
    return sessions


def session_summary(provider, session_id, rows, since, until):
    records = []
    for row in rows:
        try:
            stamp = parse_time(row.get("timestamp"))
        except ValueError:
            stamp = None
        records.append((stamp, row))
    # Keep undated records for tool-call lookup; they cannot enter the interval.
    records.sort(key=lambda pair: pair[0] if pair[0] is not None else float("-inf"))
    active = [stamp for stamp, _ in records if stamp is not None and since <= stamp < until]
    if not active:
        return None
    summary = {"provider": provider, "id": session_id, "start": min(active), "end": max(active),
               "users": [], "errors": 0, "interrupts": 0, "blocks": [], "factory": False}
    calls = {}
    response_users, event_users = [], []
    for stamp, row in records:
        inside = stamp is not None and since <= stamp < until
        kind = row.get("type")
        payload = row.get("payload") or {}
        content = row.get("message", {}).get("content") if isinstance(row.get("message"), dict) else None
        user = None
        results = []
        if provider == "claude":
            if isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "tool_use":
                        calls[block.get("id")] = command_input(block.get("input"))
                    elif block.get("type") == "tool_result":
                        results.append((block.get("tool_use_id"), block.get("content"), bool(block.get("is_error"))))
            if kind == "user" and not row.get("isMeta"):
                user = content
        elif kind == "response_item":
            if payload.get("type") in ("function_call", "custom_tool_call"):
                calls[payload.get("call_id")] = command_input(payload.get("arguments", payload.get("input")))
            elif payload.get("type") in ("function_call_output", "custom_tool_call_output"):
                value = payload.get("output")
                results.append((payload.get("call_id"), value, failed_output(value)))
            elif payload.get("type") == "message" and payload.get("role") == "user":
                user = codex_user_content(payload.get("content"))
        elif kind == "event_msg":
            if payload.get("type") == "user_message":
                text, interrupted = user_text(payload.get("message", ""))
                event_users.append((stamp, text, interrupted))
            elif inside and payload.get("type") in ("turn_aborted", "interrupted"):
                summary["interrupts"] += 1
        if user is not None:
            text, interrupted = user_text(user)
            response_users.append((stamp, text, interrupted))
        if not inside:
            continue
        for call_id, value, error in results:
            summary["errors"] += int(error)
            text = output_text(value)
            hooks = list(HOOK.finditer(text))
            for index, match in enumerate(hooks):
                end = hooks[index + 1].start() if index + 1 < len(hooks) else len(text)
                summary["blocks"].append((stamp, preview(Path(match.group(1)).name, 160),
                                          preview(text[match.end():end], 160), preview(calls.get(call_id, "unknown"), 220)))
    # Codex may mirror one turn in event_msg and response_item. Event messages
    # are actual user turns; response_item is the fallback for newer exports.
    users = event_users if event_users else response_users
    first_prompt = next((text for _, text, _ in users if text), "")
    summary["factory"] = first_prompt.startswith(FACTORY_PREFIXES)
    for stamp, text, interrupted in users:
        if stamp is not None and since <= stamp < until:
            if provider == "claude":
                summary["interrupts"] += int(interrupted)
            if text:
                summary["users"].append((stamp, preview(text, 300)))
    if provider == "codex" and not summary["interrupts"]:
        # An abort event and its injected <turn_aborted> message describe the
        # same interruption. Prefer explicit events; markers cover older logs.
        summary["interrupts"] = max(
            sum(interrupted for stamp, _, interrupted in group
                if stamp is not None and since <= stamp < until)
            for group in (event_users, response_users))
    return summary


def block_line(block):
    stamp, hook, reason, command = block
    return f"[{utc(stamp)}] hook={hook} reason={reason} command={command}"


def build_digest(home, since, until):
    if until <= since:
        raise ValueError("--until must be later than --since")
    warnings = Counter()
    summaries = []
    for (provider, session_id), session in read_sessions(Path(home), warnings).items():
        summary = session_summary(provider, session_id, session["rows"], since, until)
        if summary:
            summaries.append(summary)
    summaries.sort(key=lambda summary: (summary["start"], summary["provider"], summary["id"]))
    hooks = Counter(block[1] for summary in summaries for block in summary["blocks"])
    lines = ["# Session digest", f"Range: [{utc(since)}, {utc(until)})"]
    if warnings:
        lines.append("Warnings: " + " ".join(f"{key}={value}" for key, value in sorted(warnings.items())))
    for summary in summaries:
        header = (f"{preview(summary['provider'] + ':' + summary['id'], 200)} "
                  f"{utc(summary['start'])}→{utc(summary['end'])} user_turns={len(summary['users'])} "
                  f"tool_errors={summary['errors']} interrupts={summary['interrupts']} hook_blocks={len(summary['blocks'])}")
        lines.append("")
        if summary["factory"]:
            # One physical line, including every hook block, without prompts.
            details = "".join(" | " + block_line(block) for block in summary["blocks"])
            lines.append(f"- [factory] {header}{details}")
        else:
            lines.append("## " + header)
            lines.extend(f"- [{utc(stamp)}] {text}" for stamp, text in summary["users"])
            lines.extend("- BLOCK " + block_line(block) for block in summary["blocks"])
    lines.extend(["", "Hook blocks by hook: " + (
        ", ".join(f"{hook}={count}" for hook, count in sorted(hooks.items())) or "none"),
        f"Totals: sessions={len(summaries)} factory={sum(s['factory'] for s in summaries)} "
        f"user_turns={sum(len(s['users']) for s in summaries)} hook_blocks={sum(hooks.values())}"])
    return "\n".join(lines) + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--since", required=True, help="ISO-8601 or epoch seconds, inclusive")
    parser.add_argument("--until", help="ISO-8601 or epoch seconds, exclusive (default: now)")
    parser.add_argument("--out", type=Path, help="write Markdown here instead of stdout")
    args = parser.parse_args(argv)
    try:
        since = parse_time(args.since)
        until = parse_time(args.until) if args.until is not None else time.time()
        report = build_digest(Path.home(), since, until)
        if args.out:
            args.out.write_text(report, encoding="utf-8")
        else:
            sys.stdout.write(report)
    except (ValueError, OSError) as exc:
        parser.exit(2, "session_digest: " + preview(exc, 300) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
