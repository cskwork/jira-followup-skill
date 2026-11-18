"""Offline stand-in for jira-comment-format scripts/jira_status_check.py (tests only).

get(path, cred) is the REST v3 GET of the companion skill; here it refuses so a test can never reach Jira.
changelog(key, fetch) pages issue/<key>/changelog through the given fetch, as the real script does.
"""


def get(path, cred):
    del cred
    raise RuntimeError(f"offline stand-in: no Jira REST call in tests ({path})")


def changelog(key, fetch):
    values, start = [], 0
    while True:
        page = fetch(f"issue/{key}/changelog?startAt={start}&maxResults=100")
        values += page.get("values", [])
        start += len(page.get("values", []))
        if page.get("isLast", True) or not page.get("values") or start >= page.get("total", start):
            return values
