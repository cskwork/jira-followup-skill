#!/usr/bin/env python3
"""Print Markdown sections: runbook_section.py <file> <section-id>..."""
import re
from pathlib import Path
import sys

SECTION_ID = r"\d+(?:-[A-Za-z0-9]+)*"
HEADING = re.compile(r"^ {0,3}(#{1,6})\s+(.*)$")
NUMBER = re.compile(rf"^({SECTION_ID})(?:\.(?:\s|$)|\s|$)")
CITATION = re.compile(
    rf"(?:(RUNBOOK|ORCHESTRATOR)(?:\.md)?`?\s*)?"
    rf"§\s*({SECTION_ID})(?:\s*[~～–]\s*§?\s*({SECTION_ID}))?"
)


def extract_sections(document, sections):
    """Keep the heading and stop at the next heading of equal/higher rank."""
    lines = document.splitlines(keepends=True)
    headings = []
    fence = None
    for index, line in enumerate(lines):
        marker = re.match(r"^ {0,3}(`{3,}|~{3,})", line)
        if marker:
            value = marker[1]
            if fence is None:
                fence = value
            elif value[0] == fence[0] and len(value) >= len(fence):
                fence = None
            continue
        if fence is not None:
            continue
        heading = HEADING.match(line)
        if heading:
            number = NUMBER.match(heading[2])
            headings.append((index, len(heading[1]), number[1] if number else None))

    spans = []
    for section in dict.fromkeys(sections):
        matches = [position for position, heading in enumerate(headings) if heading[2] == section]
        if len(matches) != 1:
            reason = "missing" if not matches else "duplicate"
            raise ValueError(f"{reason} section §{section}")
        position = matches[0]
        start, rank, _ = headings[position]
        end = next((index for index, level, _ in headings[position + 1:] if level <= rank), len(lines))
        spans.append((start, end))
    # An enclosing requested section already contains its requested children.
    selected = [(start, end) for start, end in spans
                if not any(other_start <= start and end <= other_end and (start, end) != (other_start, other_end)
                           for other_start, other_end in spans)]
    return "\n\n".join("".join(lines[start:end]).rstrip() for start, end in selected)


def cited_sections(brief):
    """Only the two known documents are selectable; paths in briefs are data."""
    requested = {"RUNBOOK": ["0"], "ORCHESTRATOR": []}
    for line in brief.splitlines():
        document = "RUNBOOK"
        for match in CITATION.finditer(line):
            document = match[1] or document
            start, end = match[2], match[3]
            # §7-3-4~5 and §7-3-4~§7-3-5 both keep the shared prefix.
            prefix, _, first = start.rpartition("-")
            last = end[len(prefix) + 1:] if end and prefix and end.startswith(prefix + "-") else end
            if end is None:
                requested[document].append(start)
            elif first.isdecimal() and last.isdecimal() and int(first) <= int(last) <= int(first) + 100:
                requested[document].extend(f"{prefix}-{number}" if prefix else str(number)
                                           for number in range(int(first), int(last) + 1))
            else:
                raise ValueError(f"unsupported section range §{start}~§{end}")
    return requested


def worker_sections(skill, brief):
    chunks = []
    for document, sections in cited_sections(brief).items():
        if sections:
            source = skill / f"{document}.md"
            try:
                content = source.read_text(encoding="utf-8")
                if document == "RUNBOOK":
                    sections = resolve_steps(content, sections)
                text = extract_sections(content, sections)
            except ValueError as error:
                raise ValueError(f"{source.name}: {error}") from error
            chunks.append(f"--- {source.name} (selected sections) ---\n{text}")
    return "\n\n".join(chunks)


def resolve_steps(document, sections):
    """Existing briefs cite §7-3's numbered steps as §7-3-0 through §7-3-6."""
    resolved = []
    for section in sections:
        step = re.fullmatch(r"7-3-(\d+)", section)
        if step:
            try:
                extract_sections(document, [section])
            except ValueError as error:
                if str(error) != f"missing section §{section}":
                    raise
                parent = extract_sections(document, ["7-3"])
                if re.search(rf"(?m)^{re.escape(step[1])}\.\s+\*\*", parent):
                    section = "7-3"
        resolved.append(section)
    return resolved


def main():
    if len(sys.argv) < 3:
        print(__doc__, file=sys.stderr)
        return 2
    try:
        text = extract_sections(Path(sys.argv[1]).read_text(encoding="utf-8"), sys.argv[2:])
    except (OSError, UnicodeError, ValueError) as error:
        print(f"runbook_section: {error}", file=sys.stderr)
        return 1
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
