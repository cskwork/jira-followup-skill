"""Offline stand-in for jira-comment-format scripts/check_adf.py (tests only).

Same contract as the companion skill: walk(node, cnt, media, texts, files=None, parent=None) counts ADF
node types, collects inline images (mediaSingle media) into `media` and file cards (mediaGroup media)
into `files`. The command-line checker is not part of the stand-in.
"""


def walk(n, cnt, media, texts, files=None, parent=None):
    """media는 인라인 이미지(mediaSingle), files는 파일 카드(mediaGroup) 첨부."""
    if isinstance(n, dict):
        cnt[n.get("type")] += 1
        if n.get("type") == "media":
            (files if parent == "mediaGroup" and files is not None else media).append(
                n["attrs"].get("alt") or n["attrs"].get("id"))
        texts.append(n.get("text", ""))
        for c in n.get("content", []):
            walk(c, cnt, media, texts, files, n.get("type"))
