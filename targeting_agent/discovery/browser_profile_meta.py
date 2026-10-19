"""프로필 공개 메타태그 파서 (Phase 19N).

`creator_fit` 축이 늘 0.5(정보 없음)에 묶여 Browser 후보의 점수 상한이
79.5점에 걸렸다 — COMMENT 기준 82에 닿지 못하는 직접 원인이었다(18D.2 보정 진단).
이 모듈은 공개 프로필 페이지가 **스스로 내보내는** Open Graph 태그 문자열에서
팔로워 규모만 읽는다.

실기에서 확인되는 형태:
    og:description = '1,234 Followers, 567 Following, 89 Posts - See Instagram
                      photos and videos from 민대리 (@liam_mmgz)'

하지 않는 것: 네트워크 호출·비공개 API·응답 가로채기·쿠키 읽기.
문자열 해석만 한다(게시물 메타 파서와 같은 방식).

반올림값(15K) 처리: `creator_fit`은 구간 함수이므로 팔로워의 **규모**만 쓴다.
그래서 플랫폼이 내보낸 반올림값도 받아들이되 `approximate=True`로 표시한다.
지어낸 값이 아니라 플랫폼이 보여 주는 값이며, 정확도가 필요한 축에는 쓰지 않는다.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

# '1,234 Followers' / '15K followers' / '1.2M Followers'
_FOLLOWERS_RE = re.compile(
    r"(?<![\w.])(?P<value>\d[\d,]*(?:\.\d+)?)\s*(?P<unit>[KMkm])?\s+followers\b", re.I
)
# '@username' 끝부분
_HANDLE_RE = re.compile(r"\(@(?P<username>[A-Za-z0-9._]{1,30})\)")
# 비공개 계정 안내(한국어/영어). 보이면 Interaction 대상이 아니다.
_PRIVATE_MARKERS = (
    "this account is private",
    "비공개 계정입니다",
    "비공개 계정",
)
_UNITS = {"k": 1_000, "m": 1_000_000}


@dataclass(frozen=True)
class ProfileMeta:
    """공개 프로필 메타태그에서 읽은 값. 못 읽으면 비워 둔다(추측 금지)."""

    username: str = ""
    followers: Optional[int] = None
    is_private: bool = False
    approximate: bool = False

    @property
    def known(self) -> bool:
        return self.followers is not None

    @property
    def unreadable(self) -> bool:
        """비공개가 아닌데 팔로워를 못 읽음 — 파서/형식 불일치가 의심되는 경우."""
        return self.followers is None and not self.is_private


def _to_int(value: str, unit: Optional[str]) -> Optional[int]:
    text = (value or "").replace(",", "").strip()
    if not text:
        return None
    try:
        number = float(text)
    except ValueError:
        return None
    if unit:
        number *= _UNITS.get(unit.lower(), 1)
    return int(number) if number >= 0 else None


def parse_profile_meta(og_description: str = "", body_text: str = "") -> ProfileMeta:
    """og:description에서 팔로워 수와 비공개 여부를 읽는다."""
    desc = (og_description or "").strip()
    lowered = f"{desc} {body_text or ''}".lower()
    is_private = any(marker in lowered for marker in _PRIVATE_MARKERS)

    username = ""
    handle = _HANDLE_RE.search(desc)
    if handle:
        username = handle.group("username")

    followers: Optional[int] = None
    approximate = False
    match = _FOLLOWERS_RE.search(desc)
    if match:
        followers = _to_int(match.group("value"), match.group("unit"))
        # 단위가 붙었거나 소수점이 있으면 플랫폼이 반올림한 값이다.
        approximate = bool(match.group("unit")) or "." in match.group("value")

    return ProfileMeta(
        username=username,
        followers=followers,
        is_private=is_private,
        approximate=approximate,
    )


def profile_url(username: str, *, base_url: str) -> str:
    """공개 프로필 주소. 추측 경로를 만들지 않는다 — Instagram의 표준 프로필 경로다."""
    handle = str(username or "").strip().lstrip("@").strip("/")
    if not handle:
        return ""
    return f"{base_url.rstrip('/')}/{handle}/"
