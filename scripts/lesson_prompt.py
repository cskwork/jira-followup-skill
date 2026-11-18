"""Read-only, fail-open lesson prompt block shared by the three launchers."""
import argparse
import json
from pathlib import Path
import re
import subprocess
import sys


HEADING = '최근 14일 교훈(같은 실수 반복 금지):'
LESSON_KINDS = {'infra_fail', 'worker_fail', 'verifier_fail', 'review_rejected',
                'human_risk', 'reviewer_note'}


def _metadata(text, name, allowed=None):
    # Explicit fields only: never guess a stage from prose, title or worker role.
    values = set(re.findall(
        rf'''(?<![\w-])["']?{name}["']?\s*[:=]\s*["']?([a-z][a-z0-9_-]*)(?![\w-])''',
        text))
    if allowed is not None:
        values &= allowed
    if len(values) > 1:
        raise ValueError(f'ambiguous brief {name}')
    return next(iter(values), None)


def worker_filters(brief, requirements=None):
    text = Path(brief).read_text(encoding='utf-8')
    stage = None
    if requirements:
        manifest = json.loads(Path(requirements).read_text(encoding='utf-8'))
        stage = manifest['stage']
        if not isinstance(stage, str) or not stage.strip():
            raise ValueError('invalid requirements stage')
    else:
        stage = _metadata(text, 'stage')
    if not stage:
        raise ValueError('no explicit worker stage; index skipped')
    kind = _metadata(text, 'lesson_kind') or _metadata(text, 'kind', LESSON_KINDS)
    return stage, kind


def block(directory, *, brief=None, requirements=None):
    """Return a complete block or nothing; index failures cannot block launching."""
    try:
        command = [sys.executable, str(Path(__file__).with_name('lessons.py')),
                   'index', '--dir', str(directory)]
        if brief is not None:
            stage, kind = worker_filters(brief, requirements)
            command += ['--stage', stage]
            if kind:
                command += ['--kind', kind]
        result = subprocess.run(command, capture_output=True, text=True,
                                timeout=15, check=True)
        content = result.stdout.strip()
        return HEADING + '\n' + content if content else ''
    except Exception as error:
        # capture_output keeps even a crashing index's partial stdout out of prompts.
        print(f'lesson_prompt: {error}; launching without lesson index', file=sys.stderr)
        return ''


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dir', required=True)
    parser.add_argument('--brief')
    parser.add_argument('--requirements')
    args = parser.parse_args(argv)
    content = block(args.dir, brief=args.brief, requirements=args.requirements)
    if content:
        print(content)
    return 0


if __name__ == '__main__':
    sys.exit(main())
