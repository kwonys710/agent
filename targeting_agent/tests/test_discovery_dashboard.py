"""Discovery Health Dashboard / v0.3 Release Readiness 테스트 (Phase 19K·19L).

모두 DB만 읽는다. LLM 호출 없음, Instagram 접속 없음.
표본이 없는 비율은 0%가 아니라 "-"/INSUFFICIENT_DATA로 나와야 한다.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from targeting_agent.core.config import Config
from targeting_agent.core.database import (
    insert_candidate,
    record_interaction,
    upsert_creator,
)
from targeting_agent.core.models import ActionType, RawCandidate
from targeting_agent.dashboard import queries
from targeting_agent.dashboard.app import Page, render
from targeting_agent.discovery.query_bank import (
    QueryRunStats,
    QueryStatus,
    add_query,
    get_query,
    record_query_run,
)
from targeting_agent.learning.operations import (
    collect_freshness_metrics,
    collect_novelty_metrics,
    collect_query_metrics,
)
from targeting_agent.learning.release_report import (
    GATES_SECTION,
    NO_DATA,
    SECTION_ORDER,
    build_release_readiness,
    render_release_html,
    write_release_report,
)

NOW = datetime(2026, 10, 12, tzinfo=timezone.utc)

# query_bank / query_runs / candidate_queries 가 세는 테이블 전부.
TABLES = (
    "query_bank",
    "query_runs",
    "candidate_queries",
    "candidate_media",
    "creators",
    "action_queue",
    "interactions",
    "comment_drafts",
    "app_state",
)


# --- 헬퍼 (다른 테스트 파일을 건드리지 않고 여기서 직접 만든다) ------------
def seed_query(
    conn: sqlite3.Connection,
    query: str,
    *,
    category: str = "직장생활",
    status: QueryStatus = QueryStatus.ACTIVE,
) -> int:
    query_id = add_query(conn, query, category=category, status=status)
    conn.commit()
    assert query_id is not None
    return int(query_id)


def seed_query_run(
    conn: sqlite3.Connection,
    query_id: int,
    *,
    found: int = 10,
    collected: int = 8,
    new_candidates: int = 6,
    duplicates: int = 2,
    unique_creators: int = 5,
    scores: list[float] | None = None,
    failed: bool = False,
    at: str = "2026-10-10T00:00:00+00:00",
) -> None:
    record_query_run(
        conn,
        query_id,
        QueryRunStats(
            found=found,
            collected=collected,
            new_candidates=new_candidates,
            duplicates=duplicates,
            unique_creators=unique_creators,
            scores=list(scores or []),
            failed=failed,
        ),
        run_id="r1",
        at=at,
    )


def seed_candidate(
    conn: sqlite3.Connection,
    *,
    code: str = "AAA111",
    username: str = "creator_a",
    score: float | None = 88.0,
    posted_at: str = "2026-10-11",
    seen_count: int = 1,
) -> tuple[int, int]:
    candidate = RawCandidate(
        media_id=code,
        permalink=f"https://www.instagram.com/reel/{code}/",
        canonical_url=f"https://www.instagram.com/reel/{code}/",
        username=username,
        caption="퇴근 후 저녁 #직장인 #퇴근후",
        hashtags=["직장인", "퇴근후"],
        followers=5000,
        posted_at=posted_at,
    )
    creator_id = upsert_creator(conn, candidate)
    media_pk = insert_candidate(conn, candidate)
    assert media_pk is not None
    conn.execute(
        "UPDATE candidate_media SET target_score = ?, seen_count = ? WHERE media_pk = ?",
        (score, seen_count, int(media_pk)),
    )
    conn.commit()
    return int(media_pk), int(creator_id)


def attribute_query(conn: sqlite3.Connection, media_pk: int, query_id: int) -> None:
    from targeting_agent.discovery.query_bank import note_candidate_query

    note_candidate_query(conn, media_pk, query_id, at="2026-10-10T00:00:00+00:00")
    conn.commit()


def table_counts(conn: sqlite3.Connection) -> dict[str, int]:
    return {
        table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]) for table in TABLES
    }


def seeded_bank(conn: sqlite3.Connection) -> dict[str, int]:
    """검색어 3개 + 실행 기록 + 후보 귀속까지 깔아 둔 기본 상태."""
    good = seed_query(conn, "직장인")
    weak = seed_query(conn, "맛집", category="음식", status=QueryStatus.EXPERIMENTAL)
    never = seed_query(conn, "주말일상", category="주말")

    seed_query_run(conn, good, scores=[90.0, 80.0, 70.0])
    seed_query_run(conn, weak, found=4, collected=2, new_candidates=0, duplicates=2,
                   unique_creators=0, scores=[])

    media_pk, _ = seed_candidate(conn, code="M001", username="creator_a", score=90.0)
    repeat_pk, _ = seed_candidate(
        conn, code="M002", username="creator_b", score=60.0, seen_count=3
    )
    attribute_query(conn, media_pk, good)
    attribute_query(conn, repeat_pk, good)
    attribute_query(conn, repeat_pk, weak)
    return {"good": good, "weak": weak, "never": never, "media_pk": media_pk}


# --- 19K Discovery Health --------------------------------------------------
def test_discovery_health가_은행과_실행을_집계한다(conn: sqlite3.Connection):
    seeded_bank(conn)

    health = queries.discovery_health(conn, now=NOW)

    assert health["bank_size"] == 3
    assert health["status"]["ACTIVE"] == 2
    assert health["status"]["EXPERIMENTAL"] == 1
    assert health["never_run"] == 1          # "주말일상"은 한 번도 안 돌렸다
    assert health["categories"] == 3
    assert health["runs"] == 2
    assert health["queries_run"] == 2
    assert health["found"] == 14             # 10 + 4
    assert health["collected"] == 10         # 8 + 2
    assert health["new_candidates"] == 6
    assert health["duplicates"] == 4
    assert health["unique_creators"] == 5


def test_discovery_health에_novelty와_freshness가_들어간다(conn: sqlite3.Connection):
    seeded_bank(conn)

    health = queries.discovery_health(conn, now=NOW)

    assert health["novelty"]["candidates"] == 2
    assert health["novelty"]["repeat_candidates"] == 1      # seen_count 3인 후보
    assert health["novelty"]["multi_query_candidates"] == 1  # 두 검색어에서 발견
    assert health["freshness"]["0-3일"] == 2                 # 2026-10-11 게시
    assert health["fresh_rate"] == "100%"


def test_표본이_없으면_비율을_하이픈으로_돌려준다(conn: sqlite3.Connection):
    """은행이 비어 있으면 0%가 아니라 "-"다(없는 데이터를 0으로 단정하지 않는다)."""
    health = queries.discovery_health(conn, now=NOW)

    assert health["bank_size"] == 0
    assert health["new_rate"] == "-"
    assert health["duplicate_rate"] == "-"
    assert health["unique_creator_rate"] == "-"
    assert health["avg_utility_text"] == "-"
    assert health["novelty"]["repeat_rate"] == "-"


def test_검색어별_성과행을_만든다(conn: sqlite3.Connection):
    ids = seeded_bank(conn)
    rows = {row["query"]: row for row in queries.query_performance(conn)}

    good = rows["직장인"]
    assert good["category"] == "직장생활"
    assert good["status"] == "ACTIVE"
    assert good["run_count"] == 1
    assert good["found"] == 10
    assert good["new"] == 6
    assert good["duplicate"] == 2
    assert good["unique_creators"] == 5
    assert good["new_rate"] == "75%"           # 6 / (6 + 2)
    assert good["avg_score"] == "80.0"         # (90 + 80 + 70) / 3
    assert good["score_75_rate"] == "67%"      # 90, 80 두 건
    assert good["score_82_rate"] == "33%"      # 90 한 건
    assert good["last_run_at"] == "2026-10-10T00:00:00+00:00"
    assert get_query(conn, "직장인") is not None and ids["good"]


def test_표본이_없는_검색어는_점수칸이_하이픈이다(conn: sqlite3.Connection):
    """점수 표본이 없는 검색어에 0%를 찍지 않는다."""
    seeded_bank(conn)
    rows = {row["query"]: row for row in queries.query_performance(conn)}

    weak = rows["맛집"]            # 실행했지만 점수 표본이 없다
    never = rows["주말일상"]        # 한 번도 실행하지 않았다

    assert weak["avg_score"] == "-"
    assert weak["score_75_rate"] == "-"
    assert weak["score_82_rate"] == "-"
    assert weak["new_rate"] == "0%"            # 신규 0 / 중복 2 — 표본은 있다
    assert never["run_count"] == 0
    assert never["new_rate"] == "-"
    assert never["duplicate_rate"] == "-"
    assert never["unique_creator_rate"] == "-"
    assert never["utility"] == "-"
    assert never["last_run_at"] == "-"


def test_query_performance는_limit을_지킨다(conn: sqlite3.Connection):
    seeded_bank(conn)

    assert len(queries.query_performance(conn, limit=2)) == 2


# --- 19K Engagement --------------------------------------------------------
def seed_engagement(conn: sqlite3.Connection) -> int:
    """LIKE/COMMENT 집계용 후보 + Interaction + 댓글 후보."""
    high_pk, high_creator = seed_candidate(conn, code="E001", username="creator_h", score=90.0)
    mid_pk, mid_creator = seed_candidate(conn, code="E002", username="creator_m", score=78.0)
    seed_candidate(conn, code="E003", username="creator_l", score=60.0)

    record_interaction(
        conn,
        media_pk=high_pk,
        creator_id=high_creator,
        action_id=None,
        action_type=ActionType.LIKE,
        executor="browser",
        dry_run=False,
        success=True,
    )
    record_interaction(
        conn,
        media_pk=mid_pk,
        creator_id=mid_creator,
        action_id=None,
        action_type=ActionType.LIKE,
        executor="browser",
        dry_run=True,
        success=True,
    )
    # 실제로 올렸지만 게시 확인이 안 된 댓글(19G) — confirmed로 세지 않는다.
    record_interaction(
        conn,
        media_pk=high_pk,
        creator_id=high_creator,
        action_id=None,
        action_type=ActionType.COMMENT,
        executor="browser",
        dry_run=False,
        success=False,
        comment_text="좋은 하루!",
    )
    seed_draft(conn, high_pk, "퇴근하고 이 메뉴는 못 참죠", quality_ok=True)
    seed_draft(conn, high_pk, "광고 문의 주세요", quality_ok=False)
    conn.commit()
    return high_pk


def seed_draft(
    conn: sqlite3.Connection, media_pk: int, text: str, *, quality_ok: bool
) -> None:
    conn.execute(
        "INSERT INTO comment_drafts (media_pk, text, normalized_text, language, length, "
        "quality_ok, quality_reason, similarity_max, status, generator, generator_version, "
        "created_at) VALUES (?, ?, ?, 'ko', ?, ?, ?, 0, 'CANDIDATE', 'heuristic', '1', ?)",
        (
            int(media_pk),
            text,
            text.replace(" ", ""),
            len(text),
            1 if quality_ok else 0,
            None if quality_ok else "promo",
            "2026-10-11T00:00:00+00:00",
        ),
    )
    conn.commit()


def test_engagement_overview가_like와_comment를_센다(
    config: Config, conn: sqlite3.Connection
):
    seed_engagement(conn)

    overview = queries.engagement_overview(conn, config)

    assert overview["like"]["eligible"] == 2       # 90, 78 (기본 임계값 75)
    assert overview["like"]["executed"] == 2       # 실제 1 + dry-run 1
    assert overview["like"]["confirmed"] == 1
    assert overview["like"]["blocked"] == 0
    assert overview["comment"]["eligible"] == 1    # 90만 82 이상
    assert overview["comment"]["quality_pass"] == 1
    assert overview["comment"]["quality_reject"] == 1
    assert overview["comment"]["dry_run"] == 0
    assert overview["comment"]["confirmed"] == 0   # 게시 확인 안 됨
    assert overview["comment"]["unknown"] == 1


def test_eligible은_config_임계값을_따른다(config: Config, conn: sqlite3.Connection):
    """임계값을 Dashboard에 하드코딩하지 않는다 — config를 바꾸면 집계가 바뀐다."""
    seed_engagement(conn)
    before = queries.engagement_overview(conn, config)

    raw = {
        **config.raw,
        "actions": {
            **config.raw["actions"],
            "require_score_for_like": 85,
            "require_score_for_comment": 95,
        },
    }
    tightened = Config(raw=raw, path=config.path, base_dir=config.base_dir)
    after = queries.engagement_overview(conn, tightened)

    assert before["thresholds"] == {"like": 75, "comment": 82}
    assert after["thresholds"] == {"like": 85, "comment": 95}
    assert before["like"]["eligible"] == 2
    assert after["like"]["eligible"] == 1      # 90만 남는다
    assert after["comment"]["eligible"] == 0   # 95 이상이 없다


def test_dashboard_stats_탭에_discovery_health가_나온다(
    config: Config, conn: sqlite3.Connection
):
    seeded_bank(conn)
    seed_engagement(conn)

    page = render(conn, config, Page(tab="stats"), token="t")

    assert "Discovery Health" in page
    assert "검색어 성과" in page
    assert "직장인" in page
    assert "Feedback 누적" in page   # 기존 섹션이 그대로 살아 있다
    assert "Learning" in page


def test_은행이_비어도_stats_탭이_그려진다(config: Config, conn: sqlite3.Connection):
    page = render(conn, config, Page(tab="stats"), token="t")

    assert "Discovery Health" in page
    assert "Query Bank가 비어 있습니다" in page


# --- 19K operations 지표 ---------------------------------------------------
def test_operations에_query_novelty_freshness_지표가_붙는다(conn: sqlite3.Connection):
    seeded_bank(conn)

    query = collect_query_metrics(conn)
    novelty = collect_novelty_metrics(conn)
    freshness = collect_freshness_metrics(conn, now=NOW)

    assert query.as_rows()["한 번도 안 쓴 검색어"] == "1"
    assert query.new_rate == 6 / 10
    assert novelty.as_rows()["여러 검색어에서 발견"] == "1"
    assert freshness.as_rows()["0-3일"] == "2"
    assert collect_query_metrics(conn, recent_runs=1).runs == 1


def test_표본이_없으면_지표가_none이다(conn: sqlite3.Connection):
    query = collect_query_metrics(conn)

    assert query.new_rate is None
    assert query.duplicate_rate is None
    assert query.as_rows()["평균 Utility"] == "-"


# --- 19L Release Readiness -------------------------------------------------
def test_릴리스_보고서에_모든_섹션이_있다(config: Config, conn: sqlite3.Connection):
    seeded_bank(conn)
    seed_engagement(conn)

    readiness = build_release_readiness(conn, config, now=NOW)
    text = render_release_html(readiness)

    for title in SECTION_ORDER:
        assert title in readiness.sections, title
        assert title in text, title
    assert GATES_SECTION in text
    assert readiness.gates
    assert "LLM 호출 없음" in text


def test_데이터가_없으면_insufficient_data로_표시한다(
    config: Config, conn: sqlite3.Connection
):
    readiness = build_release_readiness(conn, config, now=NOW)
    text = render_release_html(readiness)

    assert NO_DATA in text
    assert readiness.sections["Query Bank"]["평균 Utility"] == NO_DATA
    assert readiness.sections["Target Score"]["평균 Target Score"] == NO_DATA
    assert readiness.sections["Discovery Novelty"]["반복 발견 비율"] == NO_DATA
    assert readiness.sections["Comment Readiness"]["품질 통과율"] == NO_DATA
    assert any("검색어 실행 기록이 없습니다" in gate for gate in readiness.gates)


def test_릴리스_보고서는_두_파일을_쓴다(config: Config, conn: sqlite3.Connection, tmp_path):
    raw = {
        **config.raw,
        "scheduler": {
            **config.raw["scheduler"],
            "summary": {**config.raw["scheduler"]["summary"], "output_dir": str(tmp_path)},
        },
    }
    cfg = Config(raw=raw, path=config.path, base_dir=tmp_path)
    seeded_bank(conn)

    readiness = build_release_readiness(conn, cfg, now=NOW)
    path = write_release_report(cfg, readiness, render_release_html(readiness))

    assert path.name == "v0_3_release_readiness_20261012.html"
    assert path.exists()
    assert (tmp_path / "v0_3_release_readiness_latest.html").exists()
    assert path.read_text(encoding="utf-8") == (
        tmp_path / "v0_3_release_readiness_latest.html"
    ).read_text(encoding="utf-8")


def test_릴리스_보고서는_db에_쓰지_않는다(config: Config, conn: sqlite3.Connection):
    """읽기 전용 분석이다 — 집계만 하고 상태를 바꾸지 않는다."""
    seeded_bank(conn)
    seed_engagement(conn)
    before = table_counts(conn)
    utilities = conn.execute(
        "SELECT query_id, status, utility, run_count FROM query_bank ORDER BY query_id"
    ).fetchall()

    readiness = build_release_readiness(conn, config, now=NOW)
    render_release_html(readiness)
    queries.discovery_health(conn, now=NOW)
    queries.query_performance(conn)
    queries.engagement_overview(conn, config)

    assert table_counts(conn) == before
    assert [tuple(row) for row in conn.execute(
        "SELECT query_id, status, utility, run_count FROM query_bank ORDER BY query_id"
    ).fetchall()] == [tuple(row) for row in utilities]


def test_릴리스_보고서가_임계값을_바꾸지_않는다(config: Config, conn: sqlite3.Connection):
    before_like = config.get("actions.require_score_for_like")
    before_comment = config.get("actions.require_score_for_comment")

    readiness = build_release_readiness(conn, config, now=NOW)

    assert f"Score {int(before_like)}+" in readiness.sections["LIKE Readiness"]["추천 임계값"]
    assert f"Score {int(before_comment)}+" in readiness.sections["Comment Readiness"]["추천 임계값"]
    assert config.get("actions.require_score_for_like") == before_like
    assert config.get("actions.require_score_for_comment") == before_comment


def test_보고서와_화면_값은_escape된다(config: Config, conn: sqlite3.Connection):
    """검색어/카테고리에 HTML이 들어와도 그대로 렌더되지 않는다."""
    query_id = seed_query(conn, "<script>alert(1)</script>", category="<b>음식</b>")
    seed_query_run(conn, query_id, scores=[80.0])

    text = render_release_html(build_release_readiness(conn, config, now=NOW))
    page = render(conn, config, Page(tab="stats"), token="t")

    assert "<b>음식</b>" not in text
    assert "&lt;b&gt;음식&lt;/b&gt;" in text
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page
