"""Browser Discovery → 기존 파이프라인 End-to-End (Phase 18A.3 / 18A.4).

FakeBrowser로 수집한 후보가 실제 Ingestion → CandidateProcessor → Score →
Comment 후보 → Dashboard까지 도달하는지 확인한다.

- 실제 Instagram에 접속하지 않는다.
- ai.provider=heuristic(conftest)이므로 Claude CLI도 호출하지 않는다.
- Instagram 쓰기 동작은 0건이다(Action Queue도 만들지 않는다 — 승인은 Dashboard에서).
"""
from __future__ import annotations

import sqlite3

import pytest

from targeting_agent.core.config import Config
from targeting_agent.dashboard.queries import candidate_detail, list_candidates
from targeting_agent.discovery.browser_models import SOURCE_BROWSER_SEARCH, PostDetail
from targeting_agent.discovery.browser_runner import (
    STATUS_SUCCESS,
    run_browser_discovery,
)

from .test_browser_discovery import FakeBrowser  # noqa: F401  (동일 Fake 재사용)

CAPTIONS = {
    "AAA111": "퇴근 후 저녁 만들어 먹기 #직장인 #퇴근후 #일상브이로그",
    "BBB222": "주말 카페에서 보내는 오후 #카페 #주말 #일상",
    "CCC333": "출근길 지하철 풍경 #직장인 #출근 #데일리",
}
QUERIES = ["직장인", "퇴근", "카페"]


def url_of(code: str) -> str:
    return f"https://www.instagram.com/reel/{code}/"


@pytest.fixture()
def three_query_browser() -> FakeBrowser:
    """대표 검색어 3개 × 후보 1개(과도한 browsing 금지)."""
    links = {query: [url_of(code)] for query, code in zip(QUERIES, CAPTIONS)}
    details = {
        url_of(code): PostDetail(
            permalink=url_of(code),
            username=f"creator_{code.lower()}",
            caption=caption,
            hashtags=[],
            media_type="REEL",
            like_count=300,
            comment_count=12,
        )
        for code, caption in CAPTIONS.items()
    }
    return FakeBrowser(links=links, details=details)


@pytest.fixture()
def e2e_config(config: Config) -> Config:
    raw = {
        **config.raw,
        "browser_discovery": {
            "enabled": True,
            "headless": True,
            "queries": QUERIES,
            "media_types": ["REEL"],
            "limits": {
                "max_queries_per_run": 3,
                "max_candidates_per_query": 3,
                "max_candidates_per_run": 3,
                "max_analyze_per_run": 3,
            },
            "lock": {"enabled": False},
            "debug": {"enabled": False},
        },
    }
    # base_dir은 패키지 루트를 유지한다(profiles/ 를 찾아야 한다).
    # lock·debug를 꺼 두었으므로 파일을 쓰지 않는다.
    return Config(raw=raw, path=config.path, base_dir=config.base_dir)


def test_대표검색어_3개가_분석까지_도달한다(
    e2e_config: Config, conn: sqlite3.Connection, three_query_browser: FakeBrowser
):
    result = run_browser_discovery(e2e_config, conn, browser=three_query_browser)

    assert result.status == STATUS_SUCCESS
    assert result.ok_queries == 3
    assert result.added == 3
    assert result.analyzed >= 1
    assert result.claude_calls == 0  # heuristic provider — Claude 호출 없음

    # 추출 품질: URL은 필수, username/caption 모두 확보됐다.
    assert result.collected == 3
    assert result.username_rate == 1.0
    assert result.caption_rate == 1.0
    assert result.detail_failed == 0


def test_후보가_점수와_댓글까지_만들어진다(
    e2e_config: Config, conn: sqlite3.Connection, three_query_browser: FakeBrowser
):
    run_browser_discovery(e2e_config, conn, browser=three_query_browser)

    rows = list_candidates(conn, source=SOURCE_BROWSER_SEARCH)
    assert len(rows) == 3

    scored = [row for row in rows if (row["target_score"] or 0) > 0]
    assert scored, "최소 한 건은 Target Score가 매겨져야 한다"

    detail = candidate_detail(conn, int(scored[0]["media_pk"]))
    assert detail is not None
    assert detail["canonical_url"].startswith("https://www.instagram.com/reel/")
    assert detail["source"] == SOURCE_BROWSER_SEARCH
    assert detail["analysis"] is not None
    assert len(detail["comments"]) >= 1  # 댓글 후보 생성까지 도달


def test_action_queue는_만들지_않는다(
    e2e_config: Config, conn: sqlite3.Connection, three_query_browser: FakeBrowser
):
    run_browser_discovery(e2e_config, conn, browser=three_query_browser)

    assert conn.execute("SELECT COUNT(*) AS n FROM action_queue").fetchone()["n"] == 0
    assert conn.execute("SELECT COUNT(*) AS n FROM interactions").fetchone()["n"] == 0


def test_caption이_없으면_needs_enrichment로_남고_분석되지_않는다(
    e2e_config: Config, conn: sqlite3.Connection
):
    url = url_of("DDD444")
    browser = FakeBrowser(
        links={"직장인": [url]},
        details={url: PostDetail(permalink=url, username="quiet_creator", caption="")},
    )

    result = run_browser_discovery(e2e_config, conn, browser=browser, queries=["직장인"])

    assert result.added == 1
    assert result.needs_enrichment == 1
    assert result.analyzed == 0
    assert result.caption_found == 0
    row = conn.execute("SELECT status FROM candidate_media").fetchone()
    assert row["status"] == "NEEDS_ENRICHMENT"


def test_같은_후보를_다시_수집하면_중복으로_끝난다(
    e2e_config: Config, conn: sqlite3.Connection, three_query_browser: FakeBrowser
):
    run_browser_discovery(e2e_config, conn, browser=three_query_browser)
    again = run_browser_discovery(
        e2e_config,
        conn,
        browser=FakeBrowser(
            links={q: [url_of(c)] for q, c in zip(QUERIES, CAPTIONS)},
            details=three_query_browser.details,
        ),
    )

    assert again.added == 0
    assert again.duplicate == 3
    assert again.claude_calls == 0
