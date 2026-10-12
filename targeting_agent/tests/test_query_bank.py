"""Query Bank / Planner / Novelty / Freshness 테스트 (Phase 19A·19B·19D·19E).

Instagram 접속·Claude 호출 없음. 모든 계산은 결정론적이다.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from targeting_agent.core.config import Config
from targeting_agent.discovery import freshness
from targeting_agent.discovery.novelty import (
    NoveltyStats,
    candidate_novelty,
    record_discovery,
    repeat_summary,
)
from targeting_agent.discovery.query_bank import (
    SEED_QUERIES,
    QueryRunStats,
    QueryStatus,
    add_query,
    counts_by_status,
    get_query,
    list_queries,
    queries_found_by,
    record_query_run,
    seed_bank,
    set_status,
)
from targeting_agent.discovery.query_planner import (
    DEFAULT_WEIGHTS,
    apply_cooldowns,
    compute_utility,
    plan_queries,
    planner_summary,
    refresh_utilities,
)

from .test_browser_executor import seed_candidate

NOW = datetime(2026, 10, 12, tzinfo=timezone.utc)


def run_query(
    conn: sqlite3.Connection,
    query: str,
    *,
    new: int = 0,
    duplicates: int = 0,
    creators: int = 0,
    scores: list[float] | None = None,
    times: int = 1,
) -> int:
    record = get_query(conn, query)
    assert record is not None
    for _ in range(times):
        record_query_run(
            conn,
            record.query_id,
            QueryRunStats(
                found=new + duplicates,
                collected=new + duplicates,
                new_candidates=new,
                duplicates=duplicates,
                unique_creators=creators,
                scores=list(scores or []),
            ),
        )
    return record.query_id


# --- 19A Query Bank --------------------------------------------------------
def test_seed는_카테고리별로_충분히_들어간다(conn: sqlite3.Connection):
    added = seed_bank(conn)

    assert added == len(SEED_QUERIES)
    assert added >= 50  # §9: 50~100개 수준
    categories = {record.category for record in list_queries(conn)}
    assert len(categories) == 10


def test_seed를_다시_실행해도_성과를_덮어쓰지_않는다(conn: sqlite3.Connection):
    seed_bank(conn)
    run_query(conn, "직장인", new=5, duplicates=1, creators=4, scores=[90.0])
    before = get_query(conn, "직장인")

    assert seed_bank(conn) == 0  # 추가 0건
    after = get_query(conn, "직장인")
    assert (after.run_count, after.new_count) == (before.run_count, before.new_count)


def test_같은_검색어는_중복_추가되지_않는다(conn: sqlite3.Connection):
    first = add_query(conn, "직장인", category="직장생활")
    second = add_query(conn, "직장인", category="다른카테고리")

    assert first is not None and second is None
    assert get_query(conn, "직장인").category == "직장생활"


def test_실행_결과가_누적된다(conn: sqlite3.Connection):
    seed_bank(conn)
    run_query(conn, "퇴근", new=3, duplicates=1, creators=3, scores=[85.0, 70.0, 95.0])

    record = get_query(conn, "퇴근")
    assert record.run_count == 1
    assert (record.new_count, record.duplicate_count) == (3, 1)
    assert record.new_rate == 0.75
    assert record.duplicate_rate == 0.25
    assert record.score_count == 3
    assert record.score_75_plus == 2
    assert record.score_82_plus == 2
    assert record.avg_target_score == pytest.approx(83.333, rel=1e-3)
    assert record.last_success_at is not None


def test_표본이_없으면_비율은_none이다(conn: sqlite3.Connection):
    seed_bank(conn)

    record = get_query(conn, "직장인")
    assert record.new_rate is None and record.duplicate_rate is None
    assert record.avg_target_score is None


def test_disabled는_삭제하지_않고_상태만_바꾼다(conn: sqlite3.Connection):
    seed_bank(conn)
    record = get_query(conn, "맛집")

    set_status(conn, record.query_id, QueryStatus.DISABLED, reason="운영자 제외")
    conn.commit()

    again = get_query(conn, "맛집")
    assert again is not None  # 행은 남아 있다
    assert again.status is QueryStatus.DISABLED
    assert again.status_reason == "운영자 제외"
    assert counts_by_status(conn)["DISABLED"] == 1


# --- 19B Utility / Rotation / Cooldown -------------------------------------
def test_utility는_성과가_좋을수록_높다(conn: sqlite3.Connection):
    seed_bank(conn)
    run_query(conn, "퇴근", new=9, duplicates=1, creators=9, scores=[90.0, 88.0])
    run_query(conn, "맛집", new=1, duplicates=9, creators=1, scores=[55.0, 50.0])

    good = compute_utility(get_query(conn, "퇴근"), DEFAULT_WEIGHTS)
    poor = compute_utility(get_query(conn, "맛집"), DEFAULT_WEIGHTS)

    assert good is not None and poor is not None
    assert good > poor


def test_표본이_없으면_utility는_none이다(conn: sqlite3.Connection):
    seed_bank(conn)

    assert compute_utility(get_query(conn, "직장인"), DEFAULT_WEIGHTS) is None


def test_utility는_결정론적이다(conn: sqlite3.Connection, config: Config):
    seed_bank(conn)
    run_query(conn, "퇴근", new=5, duplicates=2, creators=4, scores=[90.0])

    first = compute_utility(get_query(conn, "퇴근"))
    refresh_utilities(conn, config)
    second = get_query(conn, "퇴근").utility

    assert first == second


def test_중복만_나오는_검색어는_cooldown된다(conn: sqlite3.Connection, config: Config):
    seed_bank(conn)
    run_query(conn, "맛집", new=0, duplicates=10, creators=0, times=3)

    moved = apply_cooldowns(conn, config, now=NOW)

    assert "맛집" in moved["cooldown"]
    record = get_query(conn, "맛집")
    assert record.status is QueryStatus.COOLDOWN
    assert record.cooldown_until is not None
    assert record.status_reason in ("high_duplicate", "low_new_rate")


def test_실행이_적으면_cooldown하지_않는다(conn: sqlite3.Connection, config: Config):
    seed_bank(conn)
    run_query(conn, "맛집", new=0, duplicates=10, times=1)  # min_runs(3) 미달

    moved = apply_cooldowns(conn, config, now=NOW)

    assert moved["cooldown"] == []
    assert get_query(conn, "맛집").status is QueryStatus.ACTIVE


def test_cooldown_기간이_지나면_돌아온다(conn: sqlite3.Connection, config: Config):
    seed_bank(conn)
    record = get_query(conn, "맛집")
    past = (NOW - timedelta(hours=1)).isoformat(timespec="seconds")
    set_status(conn, record.query_id, QueryStatus.COOLDOWN, reason="high_duplicate", cooldown_until=past)
    conn.commit()

    moved = apply_cooldowns(conn, config, now=NOW)

    assert "맛집" in moved["restored"]
    assert get_query(conn, "맛집").status is QueryStatus.EXPERIMENTAL


def test_성과좋은_experimental은_active로_승격된다(conn: sqlite3.Connection, config: Config):
    add_query(conn, "퇴근 러닝", category="운동", origin="expansion", status=QueryStatus.EXPERIMENTAL)
    conn.commit()
    run_query(conn, "퇴근 러닝", new=8, duplicates=2, creators=7, scores=[88.0], times=2)

    moved = apply_cooldowns(conn, config, now=NOW)

    assert "퇴근 러닝" in moved["promoted"]
    assert get_query(conn, "퇴근 러닝").status is QueryStatus.ACTIVE


def test_매_run에_상한만큼만_고른다(conn: sqlite3.Connection, config: Config):
    seed_bank(conn)

    plan = plan_queries(conn, config, now=NOW)

    assert 1 <= len(plan.queries) <= int(config.get("query_planner.max_queries_per_run"))
    assert len(set(plan.queries)) == len(plan.queries)  # 중복 없음


def test_성과군과_탐색군을_섞는다(conn: sqlite3.Connection, config: Config):
    seed_bank(conn)
    for query in ("직장인", "퇴근", "카페투어"):
        run_query(conn, query, new=8, duplicates=1, creators=7, scores=[90.0])

    plan = plan_queries(conn, config, now=NOW)

    assert plan.performers, "성과 좋은 검색어가 포함돼야 한다"
    assert plan.explorers, "탐색용 검색어도 포함돼야 한다"


def test_직전과_같은_조합을_반복하지_않는다(conn: sqlite3.Connection, config: Config):
    seed_bank(conn)

    first = plan_queries(conn, config, now=NOW)
    second = plan_queries(conn, config, now=NOW)

    assert set(first.queries) != set(second.queries)
    assert second.repeated_batch is False


def test_은행이_부족하면_같은_조합을_허용한다(conn: sqlite3.Connection, config: Config):
    add_query(conn, "직장인", category="직장생활")
    add_query(conn, "퇴근", category="출퇴근")
    conn.commit()
    raw = {**config.raw, "query_planner": {**config.raw["query_planner"], "max_queries_per_run": 2}}
    small = Config(raw=raw, path=config.path, base_dir=config.base_dir)

    first = plan_queries(conn, small, now=NOW)
    second = plan_queries(conn, small, now=NOW)

    assert set(first.queries) == set(second.queries)
    assert second.repeated_batch is True  # fallback을 숨기지 않는다


def test_cooldown_검색어는_선택되지_않는다(conn: sqlite3.Connection, config: Config):
    seed_bank(conn)
    record = get_query(conn, "직장인")
    until = (NOW + timedelta(hours=48)).isoformat(timespec="seconds")
    set_status(conn, record.query_id, QueryStatus.COOLDOWN, reason="high_duplicate", cooldown_until=until)
    conn.commit()

    plan = plan_queries(conn, config, now=NOW)

    assert "직장인" not in plan.queries


def test_planner_요약을_볼_수_있다(conn: sqlite3.Connection, config: Config):
    seed_bank(conn)
    plan_queries(conn, config, now=NOW)

    summary = planner_summary(conn)

    assert summary["bank_size"] == len(SEED_QUERIES)
    assert summary["status"]["ACTIVE"] >= 1
    assert summary["last_batch"]


# --- 19D Novelty -----------------------------------------------------------
def test_새_후보는_first_seen이_기록된다(conn: sqlite3.Connection):
    seed_bank(conn)
    media_pk, _ = seed_candidate(conn)
    query_id = get_query(conn, "직장인").query_id
    stats = NoveltyStats()

    record_discovery(conn, media_pk, query="직장인", query_id=query_id, is_new=True, stats=stats)
    conn.commit()

    novelty = candidate_novelty(conn, media_pk)
    assert novelty.seen_count == 1
    assert novelty.first_seen_at is not None
    assert novelty.is_repeat is False
    assert stats.new_candidates == 1


def test_같은_reel이_여러_검색어에서_발견되면_행은_하나다(conn: sqlite3.Connection):
    seed_bank(conn)
    media_pk, _ = seed_candidate(conn)
    stats = NoveltyStats()

    for index, query in enumerate(("직장인", "퇴근", "회사생활")):
        record_discovery(
            conn,
            media_pk,
            query=query,
            query_id=get_query(conn, query).query_id,
            is_new=(index == 0),
            stats=stats,
        )
    conn.commit()

    assert conn.execute("SELECT COUNT(*) AS n FROM candidate_media").fetchone()["n"] == 1
    novelty = candidate_novelty(conn, media_pk)
    assert novelty.seen_count == 3
    assert sorted(novelty.queries) == ["직장인", "퇴근", "회사생활"]
    # 같은 초에 기록되면 순서는 query_id 기준이므로 집합으로 비교한다.
    assert sorted(queries_found_by(conn, media_pk)) == ["직장인", "퇴근", "회사생활"]
    assert stats.repeat_candidates == 2
    assert stats.claude_calls_saved == 2  # 반복 발견은 재분석하지 않는다


def test_반복_발견은_검색어별로_attribution된다(conn: sqlite3.Connection):
    seed_bank(conn)
    media_pk, _ = seed_candidate(conn)
    query_id = get_query(conn, "직장인").query_id

    record_discovery(conn, media_pk, query="직장인", query_id=query_id, is_new=True)
    record_discovery(conn, media_pk, query="직장인", query_id=query_id, is_new=False)
    conn.commit()

    row = conn.execute(
        "SELECT found_count FROM candidate_queries WHERE media_pk = ? AND query_id = ?",
        (media_pk, query_id),
    ).fetchone()
    assert row["found_count"] == 2  # 같은 조합은 횟수만 올린다


def test_반복_발견_요약(conn: sqlite3.Connection):
    seed_bank(conn)
    first, _ = seed_candidate(conn, code="AAA111", username="creator_a")
    second, _ = seed_candidate(conn, code="BBB222", username="creator_b")
    query_id = get_query(conn, "직장인").query_id
    record_discovery(conn, first, query="직장인", query_id=query_id, is_new=True)
    record_discovery(conn, first, query="퇴근", query_id=get_query(conn, "퇴근").query_id, is_new=False)
    record_discovery(conn, second, query="직장인", query_id=query_id, is_new=True)
    conn.commit()

    summary = repeat_summary(conn)

    assert summary["candidates"] == 2
    assert summary["repeat_candidates"] == 1
    assert summary["multi_query_candidates"] == 1


# --- 19E Freshness ---------------------------------------------------------
@pytest.mark.parametrize(
    "days, expected",
    [(0, 0), (3, 0), (4, 1), (7, 1), (8, 2), (30, 2), (31, 3), (400, 3)],
)
def test_경과일로_우선순위를_정한다(days: int, expected: int):
    posted = (NOW - timedelta(days=days)).date().isoformat()

    assert freshness.priority(posted, now=NOW) == expected


def test_posted_at을_모르면_버리지_않고_중간_우선순위다():
    assert freshness.priority(None, now=NOW) == freshness.PRIORITY_UNKNOWN
    assert freshness.priority("", now=NOW) == freshness.PRIORITY_UNKNOWN


def test_최근_게시물과_신규_creator를_먼저_처리한다():
    rows = [
        {"media_pk": 1, "creator_id": 10, "posted_at": (NOW - timedelta(days=20)).date().isoformat(), "seen_count": 1},
        {"media_pk": 2, "creator_id": 11, "posted_at": (NOW - timedelta(days=1)).date().isoformat(), "seen_count": 1},
        {"media_pk": 3, "creator_id": 12, "posted_at": (NOW - timedelta(days=1)).date().isoformat(), "seen_count": 1},
    ]
    # creator 11은 이미 많이 상대한 Creator → 같은 우선순위면 뒤로
    ordered = freshness.sort_candidates(rows, now=NOW, creator_counts={11: 5})

    assert [row["media_pk"] for row in ordered] == [3, 2, 1]


def test_freshness는_target_score를_바꾸지_않는다(conn: sqlite3.Connection):
    media_pk, _ = seed_candidate(conn)
    conn.execute("UPDATE candidate_media SET target_score = 88.0 WHERE media_pk = ?", (media_pk,))
    conn.commit()

    freshness.sort_candidates(
        conn.execute("SELECT media_pk, creator_id, posted_at, 1 AS seen_count FROM candidate_media").fetchall(),
        now=NOW,
    )

    assert conn.execute(
        "SELECT target_score FROM candidate_media WHERE media_pk = ?", (media_pk,)
    ).fetchone()["target_score"] == 88.0


def test_구간별_후보_수를_센다(conn: sqlite3.Connection):
    for index, days in enumerate((1, 5, 20, 100)):
        media_pk, _ = seed_candidate(conn, code=f"F{index:04d}", username=f"creator_{index}")
        conn.execute(
            "UPDATE candidate_media SET posted_at = ? WHERE media_pk = ?",
            ((NOW - timedelta(days=days)).date().isoformat(), media_pk),
        )
    conn.commit()

    counts = freshness.freshness_summary(conn, now=NOW)

    assert counts == {"0-3일": 1, "4-7일": 1, "8-30일": 1, "30일+": 1}
