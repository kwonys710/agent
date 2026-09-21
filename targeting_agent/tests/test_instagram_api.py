"""공식 Graph API 클라이언트 / Hashtag Discovery 테스트.

실제 네트워크 호출 없이 transport를 교체해 검증한다.
"""
from __future__ import annotations

import json
import urllib.parse
from datetime import datetime, timedelta, timezone

import pytest

from targeting_agent.actions.executors.official_api import OfficialAPIExecutor
from targeting_agent.core.exceptions import DiscoveryError, PlatformWarningError, RateLimitExceeded
from targeting_agent.core.models import ActionStatus, ActionType, QueuedAction
from targeting_agent.discovery.hashtag_discovery import HashtagDiscovery, media_to_candidate
from targeting_agent.discovery.instagram_api import (
    HASHTAG_WINDOW_LIMIT,
    GraphCredentials,
    HashtagQuota,
    InstagramGraphClient,
    iter_hashtags_within_quota,
)

CREDS = GraphCredentials(access_token="TEST_TOKEN", business_account_id="17841400000000000")


def _media(media_id: str, caption: str = "퇴근길 #직장인") -> dict:
    return {
        "id": media_id,
        "caption": caption,
        "media_type": "VIDEO",
        "permalink": f"https://www.instagram.com/reel/{media_id}/",
        "like_count": 300,
        "comments_count": 12,
        "timestamp": "2026-09-20T09:00:00+0000",
    }


class FakeTransport:
    """호출된 URL을 기록하고 미리 정해둔 응답을 돌려준다."""

    def __init__(self, responses: dict[str, dict] | None = None, default: dict | None = None):
        self.responses = responses or {}
        self.default = default if default is not None else {"data": []}
        self.calls: list[str] = []

    def __call__(self, url: str) -> dict:
        self.calls.append(url)
        for key, response in self.responses.items():
            if key in url:
                return response
        return self.default

    def call_params(self, index: int = 0) -> dict[str, str]:
        query = urllib.parse.urlparse(self.calls[index]).query
        return {k: v[0] for k, v in urllib.parse.parse_qs(query).items()}


def _client(transport: FakeTransport, conn=None) -> InstagramGraphClient:
    return InstagramGraphClient(credentials=CREDS, transport=transport, conn=conn)


# --- Credential / 상태 ---------------------------------------------------
def test_available_requires_both_credentials() -> None:
    assert _client(FakeTransport()).available() is True
    partial = InstagramGraphClient(
        credentials=GraphCredentials(access_token="T", business_account_id=None),
        transport=FakeTransport(),
    )
    assert partial.available() is False


def test_validate_token(monkeypatch) -> None:
    ok = _client(FakeTransport(default={"id": "178414", "username": "me"}))
    assert ok.validate_token() is True

    bad = _client(FakeTransport(default={"error": {"code": 190, "message": "expired"}}))
    assert bad.validate_token() is False  # PlatformWarningError를 흡수하고 False 반환


def test_access_token_not_leaked_in_errors() -> None:
    client = _client(FakeTransport(default={"error": {"code": 100, "message": "bad field"}}))
    with pytest.raises(DiscoveryError) as exc:
        client.search_hashtag_id("직장인")
    assert "TEST_TOKEN" not in str(exc.value)


# --- 오류 매핑 -----------------------------------------------------------
def test_oauth_error_raises_platform_warning() -> None:
    client = _client(FakeTransport(default={"error": {"code": 190, "message": "session expired"}}))
    with pytest.raises(PlatformWarningError):
        client.search_hashtag_id("직장인")


def test_rate_limit_error_raises_rate_limit_exceeded() -> None:
    client = _client(FakeTransport(default={"error": {"code": 4, "message": "limit reached"}}))
    with pytest.raises(RateLimitExceeded):
        client.search_hashtag_id("직장인")


# --- 해시태그 조회 -------------------------------------------------------
def test_search_hashtag_id_uses_cache() -> None:
    transport = FakeTransport({"ig_hashtag_search": {"data": [{"id": "17841562282540000"}]}})
    client = _client(transport)

    assert client.search_hashtag_id("#직장인") == "17841562282540000"
    assert client.search_hashtag_id("직장인") == "17841562282540000"
    assert len(transport.calls) == 1  # 두 번째는 캐시 사용(한도 절약)

    params = transport.call_params(0)
    assert params["q"] == "직장인"
    assert params["user_id"] == CREDS.business_account_id


def test_get_hashtag_media_paginates_until_limit() -> None:
    first_page = {
        "data": [_media("M1"), _media("M2")],
        "paging": {"next": "https://graph.facebook.com/next-page"},
    }
    transport = FakeTransport({"recent_media": first_page, "next-page": {"data": [_media("M3")]}})
    client = _client(transport)

    items = client.get_hashtag_media("HID", edge="recent_media", limit=3)
    assert [m["id"] for m in items] == ["M1", "M2", "M3"]

    params = transport.call_params(0)
    assert "username" not in params["fields"]   # 공식 API가 허용하지 않는 필드
    assert "permalink" in params["fields"]


def test_get_hashtag_media_rejects_unknown_edge() -> None:
    with pytest.raises(DiscoveryError):
        _client(FakeTransport()).get_hashtag_media("HID", edge="all_media")


# --- 7일 조회 한도 -------------------------------------------------------
def test_quota_prunes_old_entries() -> None:
    now = datetime.now(timezone.utc)
    quota = HashtagQuota(
        used={
            "최근": now.isoformat(),
            "지난주": (now - timedelta(days=8)).isoformat(),
        }
    )
    quota.prune(now)
    assert list(quota.used) == ["최근"]
    assert quota.remaining == HASHTAG_WINDOW_LIMIT - 1


def test_quota_blocks_new_hashtags_at_limit() -> None:
    now = datetime.now(timezone.utc).isoformat()
    quota = HashtagQuota(used={f"tag{i}": now for i in range(HASHTAG_WINDOW_LIMIT)})
    allowed, skipped = iter_hashtags_within_quota(["tag0", "새태그"], quota)
    assert allowed == ["tag0"]      # 이미 조회한 해시태그는 한도를 추가 소모하지 않음
    assert skipped == ["새태그"]


def test_quota_persisted_in_db(conn) -> None:
    transport = FakeTransport({"ig_hashtag_search": {"data": [{"id": "HID"}]}})
    client = _client(transport, conn=conn)

    quota = client.load_quota()
    client.search_hashtag_id("직장인", quota)
    client.save_quota(quota)

    reloaded = _client(FakeTransport(), conn=conn).load_quota()
    assert "직장인" in reloaded.used
    assert reloaded.remaining == HASHTAG_WINDOW_LIMIT - 1


# --- Discovery -----------------------------------------------------------
def test_media_to_candidate_normalizes_reel() -> None:
    candidate = media_to_candidate(_media("M1"), "직장인", ("REEL",))
    assert candidate is not None
    assert candidate.media_type == "REEL"           # VIDEO -> REEL 정규화
    assert candidate.username.startswith("unresolved:")  # username 제공 불가
    assert candidate.followers == 0
    assert candidate.source == "hashtag:직장인"
    assert candidate.extra["creator_unresolved"] is True


def test_media_to_candidate_filters_type_and_missing_id() -> None:
    assert media_to_candidate({"media_type": "IMAGE", "id": "X"}, "tag", ("REEL",)) is None
    assert media_to_candidate({"media_type": "VIDEO"}, "tag") is None


def test_hashtag_discovery_collects_candidates(conn) -> None:
    transport = FakeTransport(
        {
            "ig_hashtag_search": {"data": [{"id": "HID"}]},
            "recent_media": {"data": [_media("M1"), _media("M2")]},
        }
    )
    discovery = HashtagDiscovery(
        ["#직장인"], client=_client(transport, conn=conn), per_hashtag_limit=10
    )
    candidates = discovery.discover(limit=5)
    assert [c.media_id for c in candidates] == ["M1", "M2"]


def test_hashtag_discovery_without_credentials_returns_empty() -> None:
    client = InstagramGraphClient(
        credentials=GraphCredentials(None, None), transport=FakeTransport()
    )
    assert HashtagDiscovery(["직장인"], client=client).discover() == []


def test_hashtag_discovery_stops_on_rate_limit(conn) -> None:
    transport = FakeTransport(default={"error": {"code": 4, "message": "limit"}})
    discovery = HashtagDiscovery(["직장인", "퇴근"], client=_client(transport, conn=conn))
    assert discovery.discover() == []     # 한도 초과 시 우회하지 않고 중단


def test_hashtag_discovery_respects_max_hashtags_per_run(conn) -> None:
    transport = FakeTransport(
        {"ig_hashtag_search": {"data": [{"id": "HID"}]}, "recent_media": {"data": [_media("M1")]}}
    )
    discovery = HashtagDiscovery(
        ["a", "b", "c"], client=_client(transport, conn=conn), max_hashtags_per_run=2
    )
    discovery.discover()
    searches = [url for url in transport.calls if "ig_hashtag_search" in url]
    assert len(searches) == 2


# --- OfficialAPIExecutor -------------------------------------------------
def _action(action_type=ActionType.LIKE) -> QueuedAction:
    return QueuedAction(
        action_id=1, media_pk=1, media_id="M1", permalink="p", username="u", creator_id=1,
        action_type=action_type, target_score=90.0, priority=90, comment_text="퇴근 분위기 좋네요",
    )


def test_official_executor_never_writes() -> None:
    executor = OfficialAPIExecutor(
        dry_run=False, client=_client(FakeTransport(default={"id": "178414"}))
    )
    # 토큰이 유효해도 쓰기를 지원하지 않으므로 실행을 시작하지 않는다
    assert executor.validate_session() is False
    assert executor.execute_like(_action()).status is ActionStatus.SKIPPED
    assert executor.execute_comment(_action(ActionType.COMMENT)).status is ActionStatus.SKIPPED

    health = executor.health_check()
    assert health["supports_like"] is False
    assert "like_other_media" in health["not_supported"]
