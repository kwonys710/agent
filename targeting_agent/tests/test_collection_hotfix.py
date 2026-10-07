"""Phase 18A.5 — Candidate Collection Pipeline Hotfix.

실기 증상 재현: 검색어 1개에서 Found 10 / Collected 0 / 오류 0.
원인은 태그·프로필 그리드의 `/<username>/reel/<code>/` 형태가 Phase 12A 정규화에서
거부되고, `_collect_one`이 사유 없이 drop(silent skip)했기 때문이다.

실제 Instagram 접속 없음. Claude 호출 없음. Instagram 쓰기 없음.
"""
from __future__ import annotations

import sqlite3

import pytest

from targeting_agent.core.config import Config
from targeting_agent.core.exceptions import DiscoveryError
from targeting_agent.discovery.browser_instagram import InstagramBrowserDiscovery
from targeting_agent.discovery.browser_models import (
    SKIP_DUPLICATE_IN_RUN,
    SKIP_INVALID_URL,
    SKIP_NON_REEL,
    SOURCE_BROWSER_SEARCH,
    PostDetail,
)
from targeting_agent.discovery.browser_runner import (
    STATUS_EMPTY,
    STATUS_PARTIAL,
    STATUS_SUCCESS,
    format_result,
    run_browser_discovery,
)
from targeting_agent.discovery.url_input import normalize_instagram_url

from .test_browser_discovery import FakeBrowser, FakeProcessor

QUERY = "직장인"
GRID = "https://www.instagram.com/office_daily_kim/reel/{code}/"


def detail_for(url: str, code: str, *, username: str = "office_daily_kim", caption: str = "퇴근 후 카페 #직장인") -> PostDetail:
    return PostDetail(permalink=url, username=username, caption=caption, media_type="REEL")


def grid_browser(codes: list[str], extra_links: list[str] | None = None) -> FakeBrowser:
    """실기와 같은 형태의 그리드 링크를 돌려주는 Fake."""
    links = [GRID.format(code=code) for code in codes] + list(extra_links or [])
    details = {
        f"https://www.instagram.com/reel/{code}/": detail_for(
            f"https://www.instagram.com/reel/{code}/", code
        )
        for code in codes
    }
    return FakeBrowser(links={QUERY: links}, details=details)


# --- URL 정규화 (원인) ------------------------------------------------------
@pytest.mark.parametrize(
    "raw, canonical",
    [
        ("https://www.instagram.com/reel/AAA111/", "https://www.instagram.com/reel/AAA111/"),
        ("https://www.instagram.com/reels/AAA111/", "https://www.instagram.com/reel/AAA111/"),
        # 실기에서 실제로 들어오던 형태들
        (
            "https://www.instagram.com/office_daily_kim/reel/AAA111/",
            "https://www.instagram.com/reel/AAA111/",
        ),
        (
            "https://www.instagram.com/reels/videos/AAA111/",
            "https://www.instagram.com/reel/AAA111/",
        ),
        (
            "https://www.instagram.com/office.daily_kim/p/POST01/",
            "https://www.instagram.com/p/POST01/",
        ),
    ],
)
def test_실기_url_형태가_canonical로_모인다(raw: str, canonical: str):
    assert normalize_instagram_url(raw).canonical_url == canonical


def test_username이_경로에_있으면_읽어_둔다():
    normalized = normalize_instagram_url("https://www.instagram.com/office_daily_kim/reel/AAA111/")

    assert normalized.username == "office_daily_kim"
    assert normalize_instagram_url("https://www.instagram.com/reel/AAA111/").username is None


@pytest.mark.parametrize(
    "raw",
    [
        "https://www.instagram.com/explore/tags/%EC%A7%81%EC%9E%A5%EC%9D%B8/",
        "https://www.instagram.com/office_daily_kim/",
        "https://www.instagram.com/explore/reel/AAA111/",
        "https://www.instagram.com/stories/user/123/",
        "https://www.instagram.com/direct/inbox/",
    ],
)
def test_게시물이_아닌_경로는_여전히_거부한다(raw: str):
    with pytest.raises(DiscoveryError):
        normalize_instagram_url(raw)


def test_상대경로_href도_절대_url로_만들어진다():
    """브라우저 계층이 상대경로를 공개 절대 URL로 만든 뒤 Phase 12A에 넘긴다."""
    from targeting_agent.discovery.browser_selectors import BASE_URL

    href = "/office_daily_kim/reel/AAA111/"
    absolute = href if href.startswith("http") else BASE_URL + href

    assert normalize_instagram_url(absolute).canonical_url == (
        "https://www.instagram.com/reel/AAA111/"
    )


# --- 실기 증상 재현 --------------------------------------------------------
def test_그리드_링크_10건이_모두_수집된다():
    """실기: Found 10 → Collected 0. 수정 후: 10건 모두 수집."""
    codes = [f"CODE{index:03d}" for index in range(10)]
    browser = grid_browser(codes)
    discovery = InstagramBrowserDiscovery(browser, [QUERY], max_candidates_per_query=10)

    candidates = discovery.discover()

    assert len(candidates) == 10
    assert discovery.stats.links == 10
    assert discovery.stats.found == 10
    assert discovery.stats.collected == 10
    assert discovery.stats.skips == {}
    assert all(c.source == SOURCE_BROWSER_SEARCH for c in candidates)


def test_검색결과_10건중_reel_3건이면_3건_수집한다():
    """실기 보고와 같은 규모(링크 10건)에서 Reel만 골라 수집한다."""
    reels = ["AAA111", "BBB222", "CCC333"]
    others = [
        "https://www.instagram.com/office_daily_kim/p/POST01/",
        "https://www.instagram.com/office_daily_kim/p/POST02/",
        "https://www.instagram.com/explore/tags/%EC%A7%81%EC%9E%A5%EC%9D%B8/",
        "https://www.instagram.com/office_daily_kim/",
        "https://www.instagram.com/explore/",
        "https://www.instagram.com/direct/inbox/",
        "https://www.instagram.com/stories/office_daily_kim/123/",
    ]
    discovery = InstagramBrowserDiscovery(
        grid_browser(reels, others), [QUERY], max_candidates_per_query=20
    )

    candidates = discovery.discover()

    assert len(candidates) == 3
    assert discovery.stats.links == 10
    assert discovery.stats.found == 3
    assert discovery.stats.collected == 3
    assert discovery.stats.skips[SKIP_NON_REEL] == 2      # /p/ 게시물 2건
    assert discovery.stats.skips[SKIP_INVALID_URL] == 5   # 태그/프로필/탐색/DM/스토리
    assert discovery.stats.accounted()


def test_모든_drop은_사유가_있다():
    """silent skip 금지 — 링크 = 수집 + skip + 상세실패 가 항상 맞아야 한다."""
    reels = ["AAA111", "BBB222"]
    others = [
        "https://www.instagram.com/office_daily_kim/reel/AAA111/",  # 중복
        "https://www.instagram.com/office_daily_kim/p/POST01/",     # Reel 아님
        "https://www.instagram.com/explore/tags/x/",                # 잘못된 경로
    ]
    discovery = InstagramBrowserDiscovery(
        grid_browser(reels, others), [QUERY], max_candidates_per_query=20
    )

    discovery.discover()
    stats = discovery.stats

    assert stats.accounted(), f"설명되지 않은 drop: {stats.links} vs {stats.collected}/{stats.skips}"
    assert stats.skips[SKIP_DUPLICATE_IN_RUN] == 1
    assert stats.skips[SKIP_NON_REEL] == 1
    assert stats.skips[SKIP_INVALID_URL] == 1


def test_진단은_최대_3건만_구조화해서_남긴다():
    codes = [f"CODE{index:03d}" for index in range(6)]
    discovery = InstagramBrowserDiscovery(
        grid_browser(codes), [QUERY], max_candidates_per_query=10
    )

    discovery.discover()
    samples = discovery.stats.samples

    assert len(samples) == 3
    for sample in samples:
        assert set(sample) <= {"path", "route", "collected", "username", "caption", "skip", "detail", "media_type"}
        assert "instagram.com" not in sample["path"]  # 경로만 남긴다
        assert "cookie" not in str(sample).lower()


def test_route와_link와_reel_수를_구분해서_센다():
    discovery = InstagramBrowserDiscovery(
        grid_browser(["AAA111"], ["https://www.instagram.com/office_daily_kim/p/POST01/"]),
        [QUERY],
        max_candidates_per_query=10,
    )

    discovery.discover()

    assert discovery.stats.routes == 1   # 공개 result page 1곳
    assert discovery.stats.links == 2    # 링크 2개
    assert discovery.stats.found == 1    # 그중 Reel 1개
    assert discovery.stats.collected == 1


# --- caption / username 실패 정책 -----------------------------------------
def test_caption이_없어도_후보로_저장된다(discovery_config_18a5, conn: sqlite3.Connection):
    url = "https://www.instagram.com/reel/AAA111/"
    browser = FakeBrowser(
        links={QUERY: ["https://www.instagram.com/office_daily_kim/reel/AAA111/"]},
        details={url: PostDetail(permalink=url, username="office_daily_kim", caption="")},
    )

    result = run_browser_discovery(
        discovery_config_18a5,
        conn,
        browser=browser,
        queries=[QUERY],
        processor_factory=lambda cfg, c: FakeProcessor(c),
    )

    # caption이 없어도 수집·저장된다(캡션 실패만으로 Collected 0이 되면 안 된다).
    assert result.collected == 1
    assert result.added == 1
    assert result.caption_found == 0
    row = conn.execute("SELECT status FROM candidate_media").fetchone()
    assert row["status"] == "NEEDS_ENRICHMENT"
    # (실제 CandidateProcessor를 쓰는 NEEDS_ENRICHMENT 흐름은 test_discovery_pipeline_e2e 가 검증한다)


def test_username을_못_읽으면_경로의_username을_쓴다(discovery_config_18a5, conn: sqlite3.Connection):
    """상세에서 username을 못 읽어도 URL에 있으면 그것을 쓴다(더 엄격한 조건을 만들지 않는다)."""
    url = "https://www.instagram.com/reel/AAA111/"
    browser = FakeBrowser(
        links={QUERY: ["https://www.instagram.com/office_daily_kim/reel/AAA111/"]},
        details={url: PostDetail(permalink=url, username="", caption="퇴근 후 카페 #직장인")},
    )

    result = run_browser_discovery(
        discovery_config_18a5,
        conn,
        browser=browser,
        queries=[QUERY],
        processor_factory=lambda cfg, c: FakeProcessor(c),
    )

    assert result.collected == 1
    row = conn.execute(
        "SELECT c.username FROM candidate_media m JOIN creators c ON c.creator_id = m.creator_id"
    ).fetchone()
    assert row["username"] == "office_daily_kim"


def test_url도_username도_없으면_unresolved로_저장한다(discovery_config_18a5, conn: sqlite3.Connection):
    url = "https://www.instagram.com/reel/AAA111/"
    browser = FakeBrowser(
        links={QUERY: [url]},  # 경로에 username 없음
        details={url: PostDetail(permalink=url, username="", caption="퇴근 후 #직장인")},
    )

    result = run_browser_discovery(
        discovery_config_18a5,
        conn,
        browser=browser,
        queries=[QUERY],
        processor_factory=lambda cfg, c: FakeProcessor(c),
    )

    assert result.collected == 1  # 저장은 된다(기존 Phase 12A 최소 정책)
    row = conn.execute(
        "SELECT c.username FROM candidate_media m JOIN creators c ON c.creator_id = m.creator_id"
    ).fetchone()
    assert row["username"].startswith("unresolved:")


# --- Status 의미 -----------------------------------------------------------
def test_수집이_있으면_success(discovery_config_18a5, conn: sqlite3.Connection):
    result = run_browser_discovery(
        discovery_config_18a5,
        conn,
        browser=grid_browser(["AAA111"]),
        queries=[QUERY],
        processor_factory=lambda cfg, c: FakeProcessor(c),
    )

    assert result.status == STATUS_SUCCESS
    assert result.collected == 1


def test_reel이_없으면_empty이고_실패가_아니다(discovery_config_18a5, conn: sqlite3.Connection):
    """검색·수집 경로는 정상인데 Reel이 없는 정상 상황은 FAILED가 아니다."""
    browser = FakeBrowser(
        links={QUERY: ["https://www.instagram.com/office_daily_kim/p/POST01/"]}, details={}
    )

    result = run_browser_discovery(
        discovery_config_18a5, conn, browser=browser, queries=[QUERY], process=False
    )

    assert result.status == STATUS_EMPTY
    assert result.exit_code == 0
    assert result.found == 0
    assert result.skips[SKIP_NON_REEL] == 1


def test_일부_검색어_실패는_partial(discovery_config_18a5, conn: sqlite3.Connection):
    good = "https://www.instagram.com/office_daily_kim/reel/AAA111/"
    browser = FakeBrowser(
        links={"good": [good]},
        details={
            "https://www.instagram.com/reel/AAA111/": detail_for(
                "https://www.instagram.com/reel/AAA111/", "AAA111"
            )
        },
        fail_queries=("bad",),
    )

    result = run_browser_discovery(
        discovery_config_18a5,
        conn,
        browser=browser,
        queries=["bad", "good"],
        processor_factory=lambda cfg, c: FakeProcessor(c),
    )

    assert result.status == STATUS_PARTIAL
    assert result.collected == 1


def test_요약에_skip_사유와_진단이_보인다(discovery_config_18a5, conn: sqlite3.Connection):
    browser = grid_browser(
        ["AAA111"], ["https://www.instagram.com/office_daily_kim/p/POST01/"]
    )

    result = run_browser_discovery(
        discovery_config_18a5,
        conn,
        browser=browser,
        queries=[QUERY],
        processor_factory=lambda cfg, c: FakeProcessor(c),
    )
    text = format_result(result)

    assert "Reel 발견" in text and "수집" in text
    assert "Skip 사유" in text and SKIP_NON_REEL in text
    assert "진단(최대 3건)" in text


# --- 안전 ------------------------------------------------------------------
def test_discover_only는_claude와_쓰기가_0이다(discovery_config_18a5, conn: sqlite3.Connection):
    browser = grid_browser(["AAA111", "BBB222"])

    result = run_browser_discovery(
        discovery_config_18a5, conn, browser=browser, queries=[QUERY], process=False
    )

    assert result.collected == 2
    assert result.claude_calls == 0
    used = {call[0] for call in browser.calls}
    assert used <= {"session_state", "search", "collect", "open_post"}
    assert conn.execute("SELECT COUNT(*) AS n FROM interactions").fetchone()["n"] == 0
    assert conn.execute("SELECT COUNT(*) AS n FROM action_queue").fetchone()["n"] == 0
