"""Comment Quality Calibration 테스트 (Phase 18D.3).

Claude를 호출하지 않는다 — 저장된 댓글만 읽어 센다.
"""
from __future__ import annotations

import json
import sqlite3

from targeting_agent.core.config import Config
from targeting_agent.readiness.comments import (
    GROUNDED_RATE_WARNING,
    MIN_SAMPLES_FOR_WARNING,
    CommentSample,
    collect_comment_calibration,
    reason_family,
    score_variant,
)


def _sample(text: str, caption: str, *, hashtags: tuple[str, ...] = ()) -> CommentSample:
    return CommentSample(
        draft_id=1,
        media_pk=1,
        text=text,
        generator="claude_code",
        status="CANDIDATE",
        quality_ok=True,
        quality_reason="",
        similarity_max=0.0,
        caption=caption,
        hashtags=hashtags,
    )


# --- 거절 사유 정규화 -------------------------------------------------------
def test_값이_붙은_거절_사유를_종류로_묶는다():
    assert reason_family("duplicate(0.89)") == "duplicate"
    assert reason_family("generic:잘 보고") == "generic"
    assert reason_family("too_many_emoji(2)") == "too_many_emoji"
    assert reason_family("no_context_keyword") == "no_context_keyword"
    assert reason_family("") == ""


# --- caption 근거 -----------------------------------------------------------
def test_조사가_붙어도_같은_낱말로_본다():
    """caption의 '한강이'와 댓글의 '한강'은 같은 말이다.

    글자가 완전히 같을 때만 세면 한국어에서는 겹친 것을 대부분 놓친다.
    """
    sample = _sample("한강 근처 회사라니 점심시간에 산책하기 좋겠어요", "회사 어떤 점이 제일 좋아요? 한강이 가까워요")

    assert "한강" in sample.shared_tokens
    assert sample.grounded is True


def test_해시태그도_근거로_센다():
    sample = _sample("퇴근 후 카페 가는 루틴 좋네요", "오늘의 기록", hashtags=("퇴근", "카페"))

    assert sample.grounded is True


def test_아무_게시물에나_붙는_댓글은_근거가_없다():
    sample = _sample("오늘도 좋은 하루 보내세요", "한강에서 야근 후 퇴근길 기록")

    assert sample.shared_tokens == ()
    assert sample.grounded is False


def test_흔한_어미는_근거로_치지_않는다():
    """'너무', '정말' 같은 말이 겹쳤다고 '내용을 봤다'고 할 수는 없다."""
    sample = _sample("너무 정말 좋아요", "너무 정말 멋진 하루")

    assert sample.grounded is False


def test_짧은_글자는_근거로_치지_않는다():
    """한 글자가 겹치는 건 우연이다."""
    sample = _sample("아 그렇군요", "퇴근길 기록")

    assert sample.grounded is False


# --- DB 집계 ---------------------------------------------------------------
def _insert_draft(
    conn: sqlite3.Connection,
    *,
    text: str,
    caption: str,
    quality_ok: bool,
    reason: str = "",
    generator: str = "claude_code",
    media_pk: int | None = None,
) -> None:
    if media_pk is None:
        cursor = conn.execute(
            "INSERT INTO creators (username, status, first_seen_at, updated_at) "
            "VALUES (?, 'NEW', '2026-10-01', '2026-10-01')",
            (f"user{conn.total_changes}",),
        )
        creator_id = int(cursor.lastrowid)
        cursor = conn.execute(
            """INSERT INTO candidate_media
               (media_id, creator_id, permalink, canonical_url, media_type, caption, hashtags,
                source, status, discovered_at, updated_at)
               VALUES (?, ?, ?, ?, 'REEL', ?, '[]', 'instagram_browser_search', 'NEW',
                       '2026-10-01', '2026-10-01')""",
            (
                f"M{conn.total_changes}",
                creator_id,
                f"https://www.instagram.com/reel/M{conn.total_changes}/",
                f"https://www.instagram.com/reel/M{conn.total_changes}/",
                caption,
            ),
        )
        media_pk = int(cursor.lastrowid)
    conn.execute(
        """INSERT INTO comment_drafts
           (media_pk, text, normalized_text, language, length, quality_ok, quality_reason,
            similarity_max, status, generator, generator_version, created_at)
           VALUES (?, ?, ?, 'ko', ?, ?, ?, 0.0, ?, ?, '1', '2026-10-01')""",
        (
            media_pk,
            text,
            text,
            len(text),
            int(quality_ok),
            reason,
            "CANDIDATE" if quality_ok else "REJECTED",
            generator,
        ),
    )


def test_표본이_적으면_비율로_판단하지_않는다(conn: sqlite3.Connection, config: Config):
    _insert_draft(conn, text="좋네요", caption="퇴근길", quality_ok=False, reason="generic:좋네")

    result = collect_comment_calibration(conn, config)

    assert result.total < MIN_SAMPLES_FOR_WARNING
    assert result.prompt_change_warranted is False
    assert any("표본이" in finding for finding in result.findings)


def test_품질이_기준_안이면_prompt를_바꾸지_않는다(conn: sqlite3.Connection, config: Config):
    for index in range(12):
        _insert_draft(
            conn,
            text=f"퇴근길 풍경이 {index}번째로 좋아 보이네요",
            caption="야근 후 퇴근길 기록",
            quality_ok=True,
        )

    result = collect_comment_calibration(conn, config)

    assert result.total == 12
    assert result.prompt_change_warranted is False
    assert any("근거가 없습니다" in text for text in result.recommendations)


def test_generic_거절이_많으면_prompt_수정_근거가_된다(conn: sqlite3.Connection, config: Config):
    for index in range(6):
        _insert_draft(
            conn, text="좋은 영상이네요", caption="퇴근길 기록",
            quality_ok=False, reason="generic:좋은 영상",
        )
    for index in range(6):
        _insert_draft(conn, text="퇴근길 풍경 좋네요", caption="퇴근길 기록", quality_ok=True)

    result = collect_comment_calibration(conn, config)

    assert result.prompt_change_warranted is True
    assert any("generic" in finding for finding in result.findings)


def test_caption_근거가_낮으면_prompt_수정_근거가_된다(conn: sqlite3.Connection, config: Config):
    for index in range(12):
        _insert_draft(
            conn, text="오늘도 화이팅입니다", caption="퇴근길 기록", quality_ok=True
        )

    result = collect_comment_calibration(conn, config)

    assert result.grounded_rate is not None
    assert result.grounded_rate < GROUNDED_RATE_WARNING
    assert result.prompt_change_warranted is True


def test_거절_사유를_종류별로_센다(conn: sqlite3.Connection, config: Config):
    _insert_draft(conn, text="a", caption="c", quality_ok=False, reason="duplicate(0.89)")
    _insert_draft(conn, text="b", caption="c", quality_ok=False, reason="duplicate(0.95)")
    _insert_draft(conn, text="d", caption="c", quality_ok=False, reason="no_context_keyword")

    result = collect_comment_calibration(conn, config)

    assert result.reject_reasons == {"duplicate": 2, "no_context_keyword": 1}


# --- Prompt A/B 비교 --------------------------------------------------------
def test_두_prompt를_같은_기준으로_비교한다(config: Config):
    """Prompt를 바꿀 때 Old/New를 같은 표본·같은 게이트로 재야 한다(§26)."""
    caption = "야근 후 퇴근길 한강 산책 기록"
    old = [("좋은 영상 잘 봤습니다", caption, ("퇴근",))] * 3
    new = [("퇴근길 한강 산책이라니 저도 걷고 싶네요", caption, ("퇴근",))] * 3

    old_score = score_variant("old", old, config)
    new_score = score_variant("new", new, config)

    assert old_score.passed == 0  # generic 패턴에 걸린다
    assert new_score.passed == 3
    assert new_score.grounded_rate == 1.0
    assert "generic" in old_score.reject_reasons


def test_ab_비교는_운영_db에_아무것도_쓰지_않는다(conn: sqlite3.Connection, config: Config):
    before = conn.execute("SELECT COUNT(*) FROM comment_drafts").fetchone()[0]

    score_variant("test", [("퇴근길 풍경 좋네요", "퇴근길 기록", ())], config)

    after = conn.execute("SELECT COUNT(*) FROM comment_drafts").fetchone()[0]
    assert before == after == 0


def test_빈_묶음도_비교할_수_있다(config: Config):
    score = score_variant("empty", [], config)

    assert score.total == 0
    assert score.pass_rate is None
    assert score.grounded_rate is None
