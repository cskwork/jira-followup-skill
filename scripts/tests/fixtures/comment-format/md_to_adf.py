#!/usr/bin/env python3
"""Offline stand-in for jira-comment-format scripts/md_to_adf.py (tests only).

Same command line as the companion skill (`md_to_adf.py <in.md> -o <out.adf.json>`), but it only wraps
the text in one ADF paragraph per line. Tests never post the result.
"""
import argparse
import json


def convert(text):
    return {"type": "doc", "version": 1, "content": [
        {"type": "paragraph", "content": [{"type": "text", "text": line}]} for line in text.splitlines() if line.strip()]}


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("source")
    parser.add_argument("-o", "--output", required=True)
    args = parser.parse_args(argv)
    with open(args.source, encoding="utf-8") as src:
        doc = convert(src.read())
    with open(args.output, "w", encoding="utf-8") as out:
        json.dump(doc, out, ensure_ascii=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
