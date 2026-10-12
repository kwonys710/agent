"""Target Quality Calibration 테스트 (Phase 18D.2).

핵심은 하나다 — **정보 부족(모름)과 실제 부적합을 같은 칸에 넣지 않는다.**
실제 Instagram·Claude를 쓰지 않는다.
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from targeting_agent.core.config import Config
from targeting_agent.core.database import APPROVED_BY_AUTOPILOT, approve_action
from targeting_agent.readiness.calibration import (
    CONFIDENCE_HIGH,
    CONFIDENCE_INSUFFICIENT,
    CONFIDENCE_LOW,
    REASON_LOW_SIMILARITY,
    REASON_METADATA_MISSING,
    REASON_UNFIT,
    CandidateScore,
    collect_calibration,
)

WEIGHTS = {
    "content_similarity": 0.35,
    "creator_fit": 0.20,
    "activity": 0.15,
    "engagement": 0.15,
    "language_region": 0.10,
    "novelty": 0.05,
}


def _score(
    *,
    similarity: float = 0.9,
    unknown: tuple[str, ...] = (),
    score: float = 70.0,
    source: str = "instagram_browser_search",
) -> CandidateScore:
    components = {
        "content_similarity": similarity,
        "creator_fit": 0.5 if "creator_fit" in unknown else 0.9,
        "activity": 0.5 if "activity" in unknown else 0.9,
        "engagement": 0.5 if "engagement" in unknown else 0.9,
        "language_region": 1.0,
        "novelty": 1.0,
    }
    return CandidateScore(
        media_pk=1,
        media_id="AAA111",
        source=source,
        score=score,
        components=components,
        weights=dict(WEIGHTS),
        unknown=unknown,
    )


# --- 상한 / 여유 -----------------------------------------------------------
def test_모르는_축이_없으면_상한은_100이다():
    assert _score().ceiling == 100.0


def test_followers와_posted_at을_모르면_상한이_75로_내려간다():
    """creator_fit·engagement·activity 가중치 합 0.50이 중립값에 묶인다.

    실기에서 Browser Discovery 후보가 전부 이 상태였고, 그래서 COMMENT
    임계값(82)에 **구조적으로** 닿을 수 없었다.
    """
    item = _score(unknown=("creator_fit", "activity", "engagement"))

    assert item.ceiling == 75.0
    assert item.unknown_weight == 0.5


def test_게시_시각만_알아도_상한이_82_5로_올라간다():
    """activity(0.15)가 '모름'에서 빠지면 상한이 COMMENT 임계값을 넘어선다."""
    item = _score(unknown=("creator_fit", "engagement"))

    assert item.ceiling == 82.5


def test_빠진_정보를_채웠을_때_받을_수_있는_폭을_센다():
    item = _score(unknown=("creator_fit", "activity", "engagement"), score=68.0)

    assert item.headroom == 25.0  # 0.50 가중치 × (1.0 - 0.5) × 100
    assert item.potential_max == 93.0


# --- 정보 부족 vs 실제 부적합 ----------------------------------------------
def test_정보만_채우면_통과할_후보는_부적합이_아니다():
    item = _score(unknown=("creator_fit", "activity", "engagement"), score=68.0)

    assert item.metadata_decisive(70.0) is True
    assert item.reason(threshold=70.0) == REASON_METADATA_MISSING


def test_정보를_다_채워도_못_넘으면_실제_부적합이다():
    item = _score(similarity=0.65, unknown=(), score=65.0)

    assert item.metadata_decisive(70.0) is False
    assert item.reason(threshold=70.0) == REASON_UNFIT


def test_주제가_안_맞는_건_scoring이_아니라_discovery_문제다():
    """정보가 빠져 있어도 유사도 자체가 낮으면 그건 '모름'이 아니다."""
    item = _score(similarity=0.45, unknown=("creator_fit", "activity", "engagement"), score=55.0)

    assert item.reason(threshold=70.0) == REASON_LOW_SIMILARITY


def test_이미_통과한_후보는_정보부족으로_분류되지_않는다():
    item = _score(unknown=("creator_fit",), score=85.0)

    assert item.metadata_decisive(70.0) is False


# --- DB 집계 ---------------------------------------------------------------
def _insert(
    conn: sqlite3.Connection,
    media_id: str,
    *,
    score: float,
    followers: int,
    posted_at: str | None,
    similarity: float = 0.9,
) -> None:
    # creators.followers는 NOT NULL이라 "모름"이 0으로 저장된다(실기와 같다).
    cursor = conn.execute(
        "INSERT INTO creators (username, followers, status, first_seen_at, updated_at) "
        "VALUES (?, ?, 'NEW', '2026-10-01', '2026-10-01')",
        (f"user_{media_id}", int(followers)),
    )
    creator_id = int(cursor.lastrowid)
    cursor = conn.execute(
        """INSERT INTO candidate_media
           (media_id, creator_id, permalink, canonical_url, media_type, caption, hashtags,
            source, status, posted_at, discovered_at, updated_at)
           VALUES (?, ?, ?, ?, 'REEL', '', '[]', 'instagram_browser_search', 'SCORED',
                   ?, '2026-10-01', '2026-10-01')""",
        (
            media_id,
            creator_id,
            f"https://www.instagram.com/reel/{media_id}/",
            f"https://www.instagram.com/reel/{media_id}/",
            posted_at,
        ),
    )
    media_pk = int(cursor.lastrowid)
    unknown = []
    if not followers:
        unknown += ["creator_fit", "engagement"]
    if not posted_at:
        unknown.append("activity")
    components = {
        "content_similarity": similarity,
        "creator_fit": 0.5 if "creator_fit" in unknown else 0.9,
        "activity": 0.5 if "activity" in unknown else 0.9,
        "engagement": 0.5 if "engagement" in unknown else 0.9,
        "language_region": 1.0,
        "novelty": 1.0,
    }
    conn.execute(
        """INSERT INTO media_analysis
           (media_pk, analyzer, analyzer_version, prompt_version, payload, similarity,
            target_score, score_breakdown, analyzed_at)
           VALUES (?, 'claude_code', '1', '1', '{}', ?, ?, ?, '2026-10-01')""",
        (
            media_pk,
            similarity,
            score,
            json.dumps({"components": components, "weights": WEIGHTS, "total": score}),
        ),
    )


def test_db에서_정보없는_축을_후보별로_센다(conn: sqlite3.Connection, config: Config):
    _insert(conn, "AAA111", score=68.0, followers=0, posted_at=None)
    _insert(conn, "BBB222", score=72.0, followers=0, posted_at="2026-10-01")
    _insert(conn, "CCC333", score=90.0, followers=5000, posted_at="2026-10-01")

    result = collect_calibration(conn, config)
    by_id = {item.media_id: item for item in result.candidates}

    assert set(by_id["AAA111"].unknown) == {"creator_fit", "engagement", "activity"}
    assert set(by_id["BBB222"].unknown) == {"creator_fit", "engagement"}
    assert by_id["CCC333"].unknown == ()
    assert by_id["AAA111"].ceiling == 75.0
    assert by_id["BBB222"].ceiling == 82.5
    assert by_id["CCC333"].ceiling == 100.0


def test_상한이_임계값보다_낮으면_도달_불가로_잡는다(conn: sqlite3.Connection, config: Config):
    _insert(conn, "AAA111", score=68.0, followers=0, posted_at=None)

    result = collect_calibration(conn, config)

    assert len(result.comment_unreachable) == 1  # 상한 75 < COMMENT 82
    assert any("COMMENT 임계값" in finding for finding in result.findings)


# --- Ground Truth ----------------------------------------------------------
def test_사람_feedback이_없으면_신뢰도가_INSUFFICIENT다(conn: sqlite3.Connection, config: Config):
    _insert(conn, "AAA111", score=68.0, followers=0, posted_at=None)

    result = collect_calibration(conn, config)

    assert result.confidence == CONFIDENCE_INSUFFICIENT
    assert result.ground_truth_count == 0


def test_autopilot_자기승인은_ground_truth가_아니다(conn: sqlite3.Connection, config: Config):
    """Agent가 자기 결정을 근거로 자기 기준을 바꾸면 안 된다."""
    _insert(conn, "AAA111", score=68.0, followers=0, posted_at=None)
    conn.execute(
        """INSERT INTO action_queue
           (run_id, media_pk, creator_id, action_type, target_score, priority, status,
            executor, dry_run, created_at, updated_at)
           VALUES ('run-1', 1, 1, 'LIKE', 68.0, 1, 'PENDING', 'browser', 1,
                   '2026-10-01', '2026-10-01')"""
    )
    approve_action(conn, 1, by=APPROVED_BY_AUTOPILOT)

    result = collect_calibration(conn, config)

    assert result.human_approvals == 0
    assert result.confidence == CONFIDENCE_INSUFFICIENT


def test_사람_feedback이_적으면_신뢰도_LOW이고_임계값을_건드리지_않는다(
    conn: sqlite3.Connection, config: Config
):
    _insert(conn, "AAA111", score=68.0, followers=0, posted_at=None)
    conn.execute(
        "INSERT INTO feedback (media_pk, creator_id, feedback_type, value, created_at) "
        "VALUES (1, 1, 'GOOD_TARGET', 1.0, '2026-10-01')"
    )

    result = collect_calibration(conn, config)

    assert result.confidence == CONFIDENCE_LOW
    assert any("임계값 조정은 보류" in finding for finding in result.findings)
    assert any("유지" in text for text in result.recommendations)


def test_사람_feedback이_충분하면_신뢰도_HIGH다(conn: sqlite3.Connection, config: Config):
    _insert(conn, "AAA111", score=68.0, followers=0, posted_at=None)
    for index in range(10):
        conn.execute(
            "INSERT INTO feedback (media_pk, creator_id, feedback_type, value, created_at) "
            "VALUES (1, 1, 'GOOD_TARGET', 1.0, ?)",
            (f"2026-10-{index + 1:02d}",),
        )

    result = collect_calibration(conn, config)

    assert result.confidence == CONFIDENCE_HIGH


def test_보정은_설정을_바꾸지_않는다(conn: sqlite3.Connection, config: Config):
    """Phase 18D는 추천만 만든다 — 임계값을 자동으로 조정하지 않는다."""
    _insert(conn, "AAA111", score=55.0, followers=0, posted_at=None, similarity=0.45)
    before = (
        config.get("actions.require_score_for_like"),
        config.get("actions.require_score_for_comment"),
        config.get("scoring.minimum_target_score"),
    )

    result = collect_calibration(conn, config)

    after = (
        config.get("actions.require_score_for_like"),
        config.get("actions.require_score_for_comment"),
        config.get("scoring.minimum_target_score"),
    )
    assert before == after
    assert any("자동으로 변경되지 않습니다" in text for text in result.recommendations)


def test_저점수_원인_분포를_센다(conn: sqlite3.Connection, config: Config):
    _insert(conn, "AAA111", score=55.0, followers=0, posted_at=None, similarity=0.45)
    _insert(conn, "BBB222", score=68.0, followers=0, posted_at=None, similarity=0.90)
    _insert(conn, "CCC333", score=95.0, followers=5000, posted_at="2026-10-01")

    reasons = collect_calibration(conn, config).low_score_reasons()

    assert reasons == {REASON_LOW_SIMILARITY: 1, REASON_METADATA_MISSING: 1}


@pytest.mark.parametrize("limit", [1, 2, 20])
def test_저점수_표본은_상한건수를_지킨다(conn: sqlite3.Connection, config: Config, limit: int):
    from targeting_agent.readiness.calibration import sample_rows

    for index in range(5):
        _insert(conn, f"CODE{index}", score=60.0 + index, followers=0, posted_at=None)

    rows = sample_rows(collect_calibration(conn, config), limit=limit)

    assert len(rows) == min(limit, 5)
