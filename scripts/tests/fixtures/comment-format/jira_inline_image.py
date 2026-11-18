"""Offline stand-in for jira-comment-format scripts/jira_inline_image.py (tests only).

auth() returns "<email>:<token>" in the companion skill; here it refuses so a test never loads credentials.
"""


def auth():
    raise RuntimeError("offline stand-in: no Jira credentials in tests")
