"""입력 결측 기록 테스트 (P2-3).

"입력이 없어서 중립값"과 "실제로 낮은 값"을 구분해 breakdown에 남긴다.
실제 Instagram·Claude를 쓰지 않는다. 시계에 의존하지 않도록 posted_at은
항상 "있음/없음/파싱 불가"만 구분하고 값 자체는 검증하지 않는다.
"""
from __future__ import annotations

import json
import sqlite3

from targeting_agent.analysis.profile_analyzer import TargetProfile
from targeting_agent.analysis.scorer import TargetScorer, missing_inputs_for_row
from targeting_agent.core.config import Config
from targeting_agent.core.models import ContentAnalysis
from targeting_agent.dashboard import app, queries

BROWSER = "instagram_browser_search"


def _row(**overrides):
    base = {
        "caption": "퇴근 후 저녁 만들어 먹기",
        "followers": 5000,
        "is_private": 0,
        "posted_at": "2026-10-01",
        "like_count": 100,
        "comment_count": 5,
        "last_interacted_at": None,
    }
    base.update(overrides)
    return base


def _score(config: Config, profile: TargetProfile, row):
    analysis = ContentAnalysis(language="ko", topics=["일상"], keywords=["퇴근"])
    return TargetScorer(config, profile).score(row, analysis)


# --- 1. 입력이 전부 있으면 결측 없음 + notes 오염 없음 -----------------------
def test_입력이_전부_있으면_결측이_비어_있고_notes는_오염되지_않는다(config, profile):
    complete = _score(config, profile, _row())
    missing = _score(config, profile, _row(followers=0, posted_at="???"))

    assert complete.missing_inputs == []
    assert complete.notes  # 기존 notes(similarity:*)는 그대로 있다
    # 결측이 있어도 notes는 달라지지 않는다 — notes는 정책 판정(avoid_keyword)에 쓰인다.
    assert missing.notes == complete.notes
    for note in missing.notes:
        assert "missing" not in note
        assert "creator_fit" not in note and "activity" not in note


# --- 2. followers=0 ----------------------------------------------------------
def test_followers가_0이면_creator_fit과_engagement가_결측이다(config, profile):
    breakdown = _score(config, profile, _row(followers=0))

    assert set(breakdown.missing_inputs) == {"creator_fit", "engagement"}
    assert "activity" not in breakdown.missing_inputs
    assert breakdown.components["creator_fit"] == 0.5
    assert breakdown.components["engagement"] == 0.5


# --- 3. 비공개는 creator_fit 이 중립값이 아니다 -------------------------------
def test_비공개_계정의_creator_fit은_결측이_아니다(config, profile):
    breakdown = _score(config, profile, _row(is_private=1, followers=0))

    assert breakdown.components["creator_fit"] == 0.0  # 중립값이 아니라 0.0
    assert "creator_fit" not in breakdown.missing_inputs
    assert breakdown.missing_inputs == ["engagement"]


# --- 4. posted_at 파싱 실패 --------------------------------------------------
def test_posted_at_파싱_실패도_activity_결측이다(config, profile):
    breakdown = _score(config, profile, _row(posted_at="어제쯤"))

    assert breakdown.missing_inputs == ["activity"]
    assert breakdown.components["activity"] == 0.5


def test_posted_at이_없거나_비어_있으면_activity_결측이다():
    assert missing_inputs_for_row(_row(posted_at=None)) == ("activity",)
    assert missing_inputs_for_row(_row(posted_at="  ")) == ("activity",)
    assert missing_inputs_for_row(_row(posted_at="2026-10-01")) == ()


# --- 5. to_dict --------------------------------------------------------------
def test_to_dict에_missing_inputs가_추가되고_기존_키는_유지된다(config, profile):
    data = _score(config, profile, _row(followers=0)).to_dict()

    assert {"components", "weights", "total", "notes", "profile_version"} <= set(data)
    assert data["missing_inputs"] == ["creator_fit", "engagement"]
    json.dumps(data)  # 저장 가능한 형태


# --- 6. Dashboard 표시 -------------------------------------------------------
def _insert(
    conn: sqlite3.Connection, media_id: str, *, missing: list[str], source: str = BROWSER
) -> None:
    cursor = conn.execute(
        "INSERT INTO creators (username, followers, status, first_seen_at, updated_at) "
        "VALUES (?, 5000, 'NEW', '2026-10-01', '2026-10-01')",
        (f"user_{media_id}",),
    )
    cursor = conn.execute(
        """INSERT INTO candidate_media
           (media_id, creator_id, permalink, canonical_url, media_type, caption, hashtags,
            source, status, posted_at, discovered_at, updated_at)
           VALUES (?, ?, ?, ?, 'REEL', '', '[]', ?, 'SCORED', '2026-10-01',
                   '2026-10-01', '2026-10-01')""",
        (media_id, int(cursor.lastrowid), f"https://x/{media_id}/", f"https://x/{media_id}/", source),
    )
    components = {
        "content_similarity": 0.9,
        "creator_fit": 0.5 if "creator_fit" in missing else 0.9,
        "activity": 0.5 if "activity" in missing else 0.9,
        "engagement": 0.5 if "engagement" in missing else 0.9,
        "language_region": 1.0,
        "novelty": 1.0,
    }
    conn.execute(
        """INSERT INTO media_analysis
           (media_pk, analyzer, analyzer_version, prompt_version, payload, similarity,
            target_score, score_breakdown, analyzed_at)
           VALUES (?, 'claude_code', '1', '1', '{}', 0.9, 80, ?, '2026-10-01')""",
        (
            int(cursor.lastrowid),
            json.dumps(
                {
                    "components": components,
                    "weights": {"creator_fit": 0.2, "activity": 0.15, "engagement": 0.15},
                    "total": 80,
                    "missing_inputs": missing,
                }
            ),
        ),
    )


def test_표본이_없으면_결측률은_0퍼센트가_아니라_하이픈이다(conn, config):
    text = queries.score_missing(conn, config)

    assert (text["creator_fit"], text["activity"], text["engagement"]) == ("-", "-", "-")
    assert text["coverage"] == "-"


def test_표본이_있으면_결측률을_퍼센트로_보여_준다(conn, config):
    _insert(conn, "AAA111", missing=["creator_fit", "engagement"])
    _insert(conn, "BBB222", missing=[])

    text = queries.score_missing(conn, config)

    assert text["creator_fit"] == "50%"
    assert text["engagement"] == "50%"
    assert text["activity"] == "0%"  # 표본이 있는 0%는 진짜 0%
    assert text["coverage"] == "100% (기록 2/전체 2)"


def test_타일은_browser_후보만_분모로_삼는다(conn, config):
    """Import 후보는 애초에 followers가 없다 — 섞으면 구조적 오경보다(경고와 같은 정의)."""
    _insert(conn, "AAA111", missing=["creator_fit"], source="import:sample.csv")
    _insert(conn, "BBB222", missing=["creator_fit"], source="import:sample.csv")

    text = queries.score_missing(conn, config)

    assert text["creator_fit"] == "-"
    assert text["coverage"] == "-"
    assert text["browser_total"] == 0


def test_손상된_결측_기록_건수를_타일_쪽에서도_센다(conn, config):
    _insert(conn, "AAA111", missing=[])
    conn.execute(
        "UPDATE media_analysis SET score_breakdown = json_set(score_breakdown, "
        "'$.missing_inputs', 'creator_fit')"
    )

    assert queries.score_missing(conn, config)["corrupted"] == 1


# --- Dashboard 렌더 --------------------------------------------------------
def test_점수_막대는_입력없음_축을_구분해_표시한다():
    html_text = app._score_bars(
        {
            "components": {"creator_fit": 0.5, "activity": 0.5},
            "missing_inputs": ["creator_fit"],
            "notes": [],
        }
    )

    assert "creator_fit (입력없음)" in html_text
    assert "activity (입력없음)" not in html_text  # 진짜로 0.5인 축은 표시하지 않는다


def test_점수_막대는_손상된_missing_inputs를_무시한다():
    html_text = app._score_bars(
        {"components": {"creator_fit": 0.5}, "missing_inputs": "creator_fit", "notes": []}
    )

    assert "입력없음" not in html_text


def test_손상된_breakdown_하나가_discovery_health_전체를_죽이지_않는다(conn, config, caplog):
    _insert(conn, "AAA111", missing=[])
    conn.execute("UPDATE media_analysis SET score_breakdown = '{깨진 json'")

    with caplog.at_level("WARNING"):
        html_text = app._discovery_health(conn, config)

    assert "creator_fit 결측(browser)" in html_text
    assert "결측 비율 계산 실패" in caplog.text  # 조용히 삼키지 않는다


def test_타일은_수집기_전체가_아니라_후보_적재만_호출한다(conn, config, monkeypatch):
    """렌더마다 findings·feedback 조회까지 도는 비용을 피한다."""
    from targeting_agent.readiness import calibration

    def _boom(*args, **kwargs):
        raise AssertionError("collect_calibration을 호출하면 안 된다")

    monkeypatch.setattr(calibration, "collect_calibration", _boom)
    _insert(conn, "AAA111", missing=[])

    assert queries.score_missing(conn, config)["coverage"] == "100% (기록 1/전체 1)"
