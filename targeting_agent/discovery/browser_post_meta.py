"""게시물 공개 메타태그 파서 (Phase 18A.6).

2026 Instagram 상세 화면에는 `article` / `header` / `h1` 이 없다. 화면 DOM의
텍스트 덩어리에는 작성자 글과 **댓글·인접 Reel 본문이 함께** 들어와서, 어느
글이 이 게시물의 caption인지 DOM만으로는 확정할 수 없다.

반면 페이지가 스스로 내보내는 Open Graph 태그는 "지금 연 permalink 하나"의
작성자·본문만 담는다. 이 모듈은 그 태그 **문자열을 해석하기만** 한다 —
네트워크 호출도, 비공개 API 호출도, 응답 가로채기도 하지 않는다.

실기에서 확인된 형태:
    og:description = '15K likes, 669 comments - liam_mmgz - November 7, 2025: "본문..."'
    og:title       = 'Instagram의 민대리님 : "본문..."'

og:description 은 길이가 잘릴 수 있다. 잘린 본문은 **그대로** 돌려준다 —
없는 글자를 지어내지 않는다.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

# '... - <username> - <날짜>: "<본문>'  (본문은 끝에서 잘려 있을 수 있다)
_DESC_RE = re.compile(
    r"-\s*(?P<username>[A-Za-z0-9._]{1,30})\s*-\s*[^:]{0,40}:\s*[\"\u201c](?P<caption>.*)",
    re.S,
)
# '<숫자> likes' / '<숫자> comments' — 단위 없는 정수만 신뢰한다(K/M은 반올림값이다).
# 한쪽이 '15K'처럼 반올림돼 있어도 다른 쪽은 그대로 읽는다.
_LIKE_RE = re.compile(r"(?<![\w.])(?P<value>\d[\d,]*)\s+likes?\b", re.I)
_COMMENT_RE = re.compile(r"(?<![\w.])(?P<value>\d[\d,]*)\s+comments?\b", re.I)
# 'Instagram의 ...님 : "<본문>' / 'Instagram ...: "<본문>'
_TITLE_RE = re.compile(r"[\"\u201c](?P<caption>.*)", re.S)
# 본문 끝의 닫는 따옴표(+마침표)만 떼어 낸다. 잘린 본문에는 닫는 따옴표가 없다.
_TAIL_RE = re.compile(r"[\"\u201d]\s*\.?\s*$")


@dataclass(frozen=True)
class PostMeta:
    """공개 메타태그에서 읽은 값. 못 읽은 값은 비워 둔다(추측 금지)."""

    username: str = ""
    caption: str = ""
    like_count: Optional[int] = None
    comment_count: Optional[int] = None

    @property
    def empty(self) -> bool:
        return not (self.username or self.caption)


def _clean_caption(text: str) -> str:
    return _TAIL_RE.sub("", (text or "")).strip()


def _to_int(raw: str) -> Optional[int]:
    try:
        return int(raw.replace(",", ""))
    except (TypeError, ValueError):
        return None


def parse_post_meta(og_description: str = "", og_title: str = "") -> PostMeta:
    """og:description(1순위) · og:title(보조)에서 작성자·본문·반응 수를 읽는다."""
    username = caption = ""
    likes = comments = None

    desc = (og_description or "").strip()
    if desc:
        match = _DESC_RE.search(desc)
        if match:
            username = match.group("username")
            caption = _clean_caption(match.group("caption"))
        liked = _LIKE_RE.search(desc)
        commented = _COMMENT_RE.search(desc)
        likes = _to_int(liked.group("value")) if liked else None
        comments = _to_int(commented.group("value")) if commented else None

    if not caption:
        title = (og_title or "").strip()
        match = _TITLE_RE.search(title)
        if match:
            caption = _clean_caption(match.group("caption"))

    return PostMeta(
        username=username,
        caption=caption,
        like_count=likes,
        comment_count=comments,
    )
