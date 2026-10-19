"""Instagram Graph API 클라이언트 (읽기 전용).

공식 문서 확인 결과(2026-09 기준, Phase 8):

읽기 — 지원
- `GET /ig_hashtag_search?user_id={ig-user-id}&q={hashtag}` → 해시태그 node ID
- `GET /{hashtag-id}/top_media?user_id={ig-user-id}&fields=...`
- `GET /{hashtag-id}/recent_media?user_id={ig-user-id}&fields=...` (최근 24시간 공개 게시물만)
- 권한: instagram_basic + Instagram Public Content Access(앱 심사 필요),
  Instagram Business/Creator 계정 필요
- 제한: **7일 동안 고유 해시태그 30개**, 페이지당 최대 50건,
  반환 media 객체에 **username 필드를 요청할 수 없음**

쓰기 — 미지원
- 타인 게시물에 대한 좋아요/댓글 작성 엔드포인트는 공식 API에 없다.
  (공개 댓글 작성 엔드포인트는 이미 제거되었고, 이후 추가된 engagement 기능도
   본인 소유 콘텐츠 기준으로 안내된다.)
  → OfficialAPIExecutor는 쓰기 동작을 수행하지 않는다.

구현 원칙:
- 외부 HTTP 패키지를 추가하지 않고 표준 라이브러리(urllib)만 사용한다.
- Access Token은 환경변수에서만 읽고, 로그/DB/예외 메시지에 남기지 않는다.
- 해시태그 조회 수 제한은 **우회 대상이 아니라 준수 대상**이다.
  로컬에서 7일 창 사용량을 세어 한도에 도달하면 조회를 중단한다.
"""
from __future__ import annotations

import json
import os
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable, Optional, Sequence

from ..core.exceptions import DiscoveryError, PlatformWarningError, RateLimitExceeded
from ..core.logger import get_logger

logger = get_logger("discovery.instagram_api")

GRAPH_API_VERSION = "v21.0"
GRAPH_API_BASE = f"https://graph.facebook.com/{GRAPH_API_VERSION}"

# 문서상 해시태그 조회 한도: 7일 동안 고유 해시태그 30개
HASHTAG_WINDOW_DAYS = 7
HASHTAG_WINDOW_LIMIT = 30
# recent_media/top_media 페이지당 최대 건수
MAX_PAGE_LIMIT = 50

# hashtag media 에서 요청 가능한 필드(username은 요청 불가)
MEDIA_FIELDS = (
    "id",
    "caption",
    "media_type",
    "permalink",
    "like_count",
    "comments_count",
    "timestamp",
)

# Graph API 오류 코드
OAUTH_ERROR_CODES = {190, 102, 463, 467}          # 토큰 만료/재인증 필요
RATE_LIMIT_ERROR_CODES = {4, 17, 32, 613, 80007}  # 호출 한도


@dataclass(frozen=True)
class GraphCredentials:
    """환경변수에서 읽은 Graph API Credential (평문 저장/로그 금지)."""

    access_token: Optional[str]
    business_account_id: Optional[str]

    @property
    def is_complete(self) -> bool:
        return bool(self.access_token and self.business_account_id)


def load_credentials() -> GraphCredentials:
    """.env/환경변수에서 Credential을 읽는다. 값 자체는 절대 로깅하지 않는다."""
    return GraphCredentials(
        access_token=os.environ.get("IG_ACCESS_TOKEN") or None,
        business_account_id=os.environ.get("IG_BUSINESS_ACCOUNT_ID") or None,
    )


@dataclass
class HashtagQuota:
    """7일 창 기준 고유 해시태그 조회 사용량.

    Graph API가 세는 값을 그대로 알 수는 없으므로, 로컬 기록으로 보수적으로 관리한다.
    """

    used: dict[str, str] = field(default_factory=dict)  # hashtag -> 마지막 조회 시각(ISO)

    @classmethod
    def from_json(cls, raw: Optional[str]) -> "HashtagQuota":
        if not raw:
            return cls()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return cls()
        return cls(used={str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {})

    def to_json(self) -> str:
        return json.dumps(self.used, ensure_ascii=False)

    def prune(self, now: Optional[datetime] = None) -> None:
        """7일이 지난 기록은 창에서 제외한다."""
        now = now or datetime.now(timezone.utc)
        cutoff = now - timedelta(days=HASHTAG_WINDOW_DAYS)
        alive = {}
        for name, stamp in self.used.items():
            try:
                queried_at = datetime.fromisoformat(stamp)
            except ValueError:
                continue
            if queried_at.tzinfo is None:
                queried_at = queried_at.replace(tzinfo=timezone.utc)
            if queried_at >= cutoff:
                alive[name] = stamp
        self.used = alive

    @property
    def remaining(self) -> int:
        return max(0, HASHTAG_WINDOW_LIMIT - len(self.used))

    def allows(self, hashtag: str) -> bool:
        """이미 창 안에서 조회한 해시태그는 한도를 추가로 소모하지 않는다."""
        return hashtag in self.used or self.remaining > 0

    def record(self, hashtag: str, now: Optional[datetime] = None) -> None:
        self.used[hashtag] = (now or datetime.now(timezone.utc)).isoformat(timespec="seconds")


Transport = Callable[[str], dict[str, Any]]
"""URL을 받아 JSON 응답(dict)을 돌려주는 호출자. 테스트에서 교체한다."""


def _default_transport(url: str, timeout: int = 15) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:  # 오류 본문에도 Graph API 오류 정보가 들어있다
        try:
            return json.loads(exc.read().decode("utf-8"))
        except (ValueError, OSError):
            raise DiscoveryError(f"Graph API HTTP {exc.code} 오류") from exc
    except urllib.error.URLError as exc:
        raise DiscoveryError(f"Graph API 연결 실패: {exc.reason}") from exc


class InstagramGraphClient:
    """Hashtag Search 등 읽기 전용 호출을 담당한다."""

    def __init__(
        self,
        credentials: Optional[GraphCredentials] = None,
        transport: Optional[Transport] = None,
        conn: Optional[sqlite3.Connection] = None,
    ) -> None:
        self.credentials = credentials or load_credentials()
        self._transport = transport or _default_transport
        self._conn = conn
        self._hashtag_ids: dict[str, str] = {}

    # --- 상태 -----------------------------------------------------------
    def available(self) -> bool:
        if not self.credentials.is_complete:
            logger.info(
                "Instagram Graph API Credential이 없습니다 "
                "(IG_ACCESS_TOKEN / IG_BUSINESS_ACCOUNT_ID). 공식 API 기능은 비활성화됩니다."
            )
            return False
        return True

    def validate_token(self) -> bool:
        """IG User 노드를 한 번 조회해 토큰이 실제로 동작하는지 확인한다."""
        if not self.available():
            return False
        try:
            payload = self._get(str(self.credentials.business_account_id), {"fields": "id,username"})
        except (DiscoveryError, PlatformWarningError, RateLimitExceeded) as exc:
            logger.warning("Graph API 토큰 확인 실패: %s", exc)
            return False
        return bool(payload.get("id"))

    def health_check(self) -> dict[str, object]:
        quota = self.load_quota()
        return {
            "credentials_present": self.credentials.is_complete,
            "base_url": GRAPH_API_BASE,
            "supported": ["hashtag_search", "hashtag_top_media", "hashtag_recent_media"],
            "not_supported": ["like_other_media", "comment_other_media"],
            "hashtag_quota_used": len(quota.used),
            "hashtag_quota_limit": HASHTAG_WINDOW_LIMIT,
        }

    # --- 해시태그 조회 한도 -----------------------------------------------
    def load_quota(self) -> HashtagQuota:
        raw = None
        if self._conn is not None:
            from ..core.database import get_state

            raw = get_state(self._conn, "hashtag_quota")
        quota = HashtagQuota.from_json(raw)
        quota.prune()
        return quota

    def save_quota(self, quota: HashtagQuota) -> None:
        if self._conn is None:
            return
        from ..core.database import set_state, transaction

        with transaction(self._conn):
            set_state(self._conn, "hashtag_quota", quota.to_json())

    # --- 호출 -----------------------------------------------------------
    def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        query = {k: v for k, v in params.items() if v not in (None, "")}
        query["access_token"] = self.credentials.access_token
        url = f"{GRAPH_API_BASE}/{path.lstrip('/')}?{urllib.parse.urlencode(query)}"
        payload = self._transport(url)
        if not isinstance(payload, dict):
            raise DiscoveryError("Graph API 응답 형식이 올바르지 않습니다.")
        if "error" in payload:
            self._raise_api_error(payload["error"])
        return payload

    @staticmethod
    def _raise_api_error(error: Any) -> None:
        """Graph API 오류를 내부 예외로 변환한다(토큰 값은 노출하지 않는다)."""
        detail = error if isinstance(error, dict) else {}
        code = int(detail.get("code") or 0)
        message = str(detail.get("message") or "알 수 없는 Graph API 오류")
        if code in OAUTH_ERROR_CODES:
            raise PlatformWarningError(f"인증 재확인 필요(code={code}): {message}")
        if code in RATE_LIMIT_ERROR_CODES:
            raise RateLimitExceeded(f"Graph API 호출 한도(code={code}): {message}")
        raise DiscoveryError(f"Graph API 오류(code={code}): {message}")

    def search_hashtag_id(self, hashtag: str, quota: Optional[HashtagQuota] = None) -> Optional[str]:
        """해시태그 이름으로 node ID를 조회한다(세션 내 캐시 사용)."""
        name = hashtag.lstrip("#").strip()
        if not name:
            return None
        if name in self._hashtag_ids:
            return self._hashtag_ids[name]

        payload = self._get(
            "ig_hashtag_search",
            {"user_id": self.credentials.business_account_id, "q": name},
        )
        data = payload.get("data") or []
        if not data:
            logger.warning("해시태그를 찾지 못했습니다: #%s", name)
            return None
        hashtag_id = str(data[0].get("id"))
        self._hashtag_ids[name] = hashtag_id
        if quota is not None:
            quota.record(name)
        return hashtag_id

    def get_hashtag_media(
        self,
        hashtag_id: str,
        *,
        edge: str = "recent_media",
        limit: int = 25,
        fields: Sequence[str] = MEDIA_FIELDS,
    ) -> list[dict[str, Any]]:
        """해시태그의 top_media / recent_media를 가져온다(페이지네이션 포함)."""
        if edge not in ("top_media", "recent_media"):
            raise DiscoveryError(f"지원하지 않는 edge: {edge}")

        remaining = max(0, int(limit))
        results: list[dict[str, Any]] = []
        payload = self._get(
            f"{hashtag_id}/{edge}",
            {
                "user_id": self.credentials.business_account_id,
                "fields": ",".join(fields),
                "limit": min(remaining, MAX_PAGE_LIMIT) or MAX_PAGE_LIMIT,
            },
        )
        while True:
            items = payload.get("data") or []
            for item in items:
                if isinstance(item, dict):
                    results.append(item)
                if len(results) >= remaining:
                    return results
            next_url = (((payload.get("paging") or {}).get("next")) or "")
            if not next_url or len(results) >= remaining:
                return results
            payload = self._transport(next_url)
            if "error" in payload:
                self._raise_api_error(payload["error"])


def iter_hashtags_within_quota(
    hashtags: Iterable[str], quota: HashtagQuota
) -> tuple[list[str], list[str]]:
    """한도 안에서 조회 가능한 해시태그와, 한도 때문에 건너뛸 해시태그를 나눈다."""
    allowed: list[str] = []
    skipped: list[str] = []
    for hashtag in hashtags:
        name = str(hashtag).lstrip("#").strip()
        if not name:
            continue
        if name in quota.used or len(set(allowed) | set(quota.used)) < HASHTAG_WINDOW_LIMIT:
            allowed.append(name)
        else:
            skipped.append(name)
    return allowed, skipped
