"""Instagram URL 입력 검증·정규화 (Phase 12A).

이 모듈은 **URL을 해석하기만 한다.** 페이지를 열지 않고, 로그인하지 않으며,
스크래핑하지 않는다. URL에 없는 정보(username, caption, 팔로워 수 등)는
추측하지 않고 비워 둔다 — 이후 Enrichment 단계에서 채운다.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse

from ..core.exceptions import DiscoveryError
from ..core.models import RawCandidate

ALLOWED_HOSTS = {"instagram.com", "www.instagram.com", "m.instagram.com"}
# 지원 path (Phase 18A.5에서 실제 Instagram 화면의 형태를 추가했다):
#   /reel/<code>/           게시물 직접 링크
#   /reels/<code>/          구형/대체 형태
#   /reels/videos/<code>/   Reels 탭 딥링크
#   /p/<code>/, /tv/<code>/ 일반 게시물·IGTV
#   /<username>/reel/<code>/  태그·프로필 그리드에서 실제로 쓰이는 형태 ← 이게 빠져 있었다
# 어느 형태든 canonical은 https://www.instagram.com/reel|p|tv/<code>/ 하나로 모은다.
PATH_RE = re.compile(
    r"^/(?:(?P<username>[A-Za-z0-9._]+)/)?"
    r"(?P<kind>reel|reels|p|tv)/(?:videos/)?"
    r"(?P<shortcode>[0-9A-Za-z_-]+)/?$"
)
# 사용자 이름 자리에 올 수 없는 Instagram 예약 경로(오탐 방지).
RESERVED_SEGMENTS = {"explore", "reel", "reels", "p", "tv", "stories", "direct", "accounts", "s"}

KIND_TO_MEDIA_TYPE = {"reel": "REEL", "reels": "REEL", "tv": "REEL", "p": "POST"}
SOURCE_MANUAL_URL = "manual_url"
SOURCE_CSV_INBOX = "csv_inbox"


@dataclass(frozen=True)
class NormalizedUrl:
    """정규화된 Instagram 게시물 URL."""

    canonical_url: str
    shortcode: str
    kind: str
    # URL 경로에 username이 들어 있던 경우에만 채운다(추측하지 않는다).
    username: Optional[str] = None

    @property
    def media_type(self) -> str:
        return KIND_TO_MEDIA_TYPE.get(self.kind, "REEL")


def normalize_instagram_url(raw_url: str) -> NormalizedUrl:
    """Instagram 게시물 URL을 canonical 형태로 정규화한다.

    - query string / fragment(`?utm_source=...`, `#...`)는 제거한다 → 중복 방지
    - 호스트는 `www.instagram.com`으로 통일한다
    - 지원하지 않는 URL이면 DiscoveryError를 던진다(해당 입력만 실패시키기 위함)
    """
    text = (raw_url or "").strip().strip('"').strip("'")
    if not text:
        raise DiscoveryError("URL이 비어 있습니다.")

    if "://" not in text:
        text = "https://" + text.lstrip("/")

    parsed = urlparse(text)
    if parsed.scheme not in ("http", "https"):
        raise DiscoveryError(f"지원하지 않는 scheme입니다: {parsed.scheme or '(없음)'}")

    host = (parsed.hostname or "").lower()
    if host not in ALLOWED_HOSTS:
        raise DiscoveryError(f"Instagram URL이 아닙니다: {host or text}")

    match = PATH_RE.match(parsed.path if parsed.path.endswith("/") else parsed.path + "/")
    if not match:
        raise DiscoveryError(f"지원하지 않는 Instagram 경로입니다: {parsed.path or '/'}")

    kind = match.group("kind")
    shortcode = match.group("shortcode")
    username = match.group("username")
    if username and username.lower() in RESERVED_SEGMENTS:
        raise DiscoveryError(f"지원하지 않는 Instagram 경로입니다: {parsed.path or '/'}")
    # /reels/ 는 /reel/ 로 통일한다(같은 게시물이 두 형태로 중복되지 않도록).
    # /<username>/reel/<code>/ 도 같은 canonical로 모은다 → 경로 형태가 달라도 중복 판정이 된다.
    canonical_kind = "reel" if kind in ("reel", "reels") else kind
    return NormalizedUrl(
        canonical_url=f"https://www.instagram.com/{canonical_kind}/{shortcode}/",
        shortcode=shortcode,
        kind=kind,
        username=username or None,
    )


def candidate_from_url(
    raw_url: str,
    *,
    username: Optional[str] = None,
    caption: str = "",
    note: str = "",
    source: str = SOURCE_MANUAL_URL,
) -> RawCandidate:
    """URL(+선택 정보)로 후보를 만든다. 없는 정보는 채우지 않는다.

    - `media_id`는 내부 식별자로 shortcode를 쓴다. Instagram의 숫자 media_id가
      아니므로 `instagram_media_id`는 비워 둔다(이후 API로 확인되면 채운다).
    - username을 모르면 `unresolved:<shortcode>`로 두어 Creator 한도가
      잘못 적용되지 않게 한다(v0.1 Action Queue 가드와 동일한 규칙).
    """
    normalized = normalize_instagram_url(raw_url)
    # URL 경로에 username이 들어 있으면(/<username>/reel/<code>/) 보조 정보로 쓴다.
    resolved_username = (username or normalized.username or "").strip().lstrip("@")
    from .hashtag_discovery import UNRESOLVED_PREFIX  # 동일 규칙 재사용

    from .base import extract_hashtags

    return RawCandidate(
        media_id=normalized.shortcode,
        permalink=normalized.canonical_url,
        username=resolved_username or f"{UNRESOLVED_PREFIX}{normalized.shortcode}",
        caption=caption or "",
        hashtags=extract_hashtags(caption) if caption else [],
        media_type=normalized.media_type,
        canonical_url=normalized.canonical_url,
        instagram_media_id=None,
        note=note,
        source=source,
        extra={"creator_unresolved": not bool(resolved_username)},
    )
