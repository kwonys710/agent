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
# 반응 수가 없는 게시물은 '<username> - <날짜>: "<본문>' 처럼 앞의 '-' 없이
# 시작한다(실기 확인). 그래서 문자열 맨 앞도 시작점으로 받아들인다.
_HEAD = r"(?:^|-)\s*"
_DESC_RE = re.compile(
    _HEAD + r"(?P<username>[A-Za-z0-9._]{1,30})\s*-\s*[^:]{0,40}:\s*[\"\u201c](?P<caption>.*)",
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


# '- <username> - November 7, 2025:' 의 날짜 부분.
# Instagram은 한국어 UI에서도 이 태그를 영어로 내보낸다(실기 확인).
# 다른 형식이면 읽지 않고 비워 둔다 — 날짜를 지어내는 것이 모르는 것보다 나쁘다.
_DATE_RE = re.compile(
    _HEAD + r"[A-Za-z0-9._]{1,30}\s*-\s*"
    r"(?P<month>January|February|March|April|May|June|July|August|September|October|November|December)"
    r"\s+(?P<day>\d{1,2}),\s*(?P<year>\d{4})\s*:",
)
_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
}


@dataclass(frozen=True)
class PostMeta:
    """공개 메타태그에서 읽은 값. 못 읽은 값은 비워 둔다(추측 금지)."""

    username: str = ""
    caption: str = ""
    like_count: Optional[int] = None
    comment_count: Optional[int] = None
    # 'YYYY-MM-DD'. 못 읽으면 빈 문자열 — Scorer가 '모른다'로 다룬다.
    posted_at: str = ""

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


def _parse_posted_at(description: str) -> str:
    """게시 날짜를 'YYYY-MM-DD'로 읽는다. 형식이 다르면 빈 문자열."""
    match = _DATE_RE.search(description or "")
    if not match:
        return ""
    month = _MONTHS.get(match.group("month").lower())
    day = int(match.group("day"))
    year = int(match.group("year"))
    if not month or not 1 <= day <= 31:
        return ""
    return f"{year:04d}-{month:02d}-{day:02d}"


def parse_post_meta(og_description: str = "", og_title: str = "") -> PostMeta:
    """og:description(1순위) · og:title(보조)에서 작성자·본문·반응 수·날짜를 읽는다."""
    username = caption = posted_at = ""
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
        posted_at = _parse_posted_at(desc)

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
        posted_at=posted_at,
    )
