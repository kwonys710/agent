"""Phase 12A — Candidate Input Fallback 테스트.

CLI 단건 입력 / Inbox CSV / URL 정규화 / 중복 / 부분정보 저장 / 행 단위 오류 격리.
"""
from __future__ import annotations

import pytest

from targeting_agent.core.exceptions import DiscoveryError
from targeting_agent.core.models import MediaStatus
from targeting_agent.discovery.ingest import (
    ADDED,
    DUPLICATE,
    ERROR,
    INVALID,
    CandidateIngestor,
    format_summary,
)
from targeting_agent.discovery.url_input import candidate_from_url, normalize_instagram_url

CANONICAL = "https://www.instagram.com/reel/ABC123/"


def _rows(conn, sql: str, *params):
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


# --- URL Validation / Normalization --------------------------------------
@pytest.mark.parametrize(
    "raw",
    [
        "https://www.instagram.com/reel/ABC123/",
        "https://www.instagram.com/reel/ABC123",
        "https://instagram.com/reel/ABC123/",
        "https://m.instagram.com/reels/ABC123/",
        "https://www.instagram.com/reel/ABC123/?utm_source=ig_web&igshid=xyz",
        "https://www.instagram.com/reel/ABC123/#comments",
        "  www.instagram.com/reel/ABC123/  ",
    ],
)
def test_normalization_maps_variants_to_one_canonical(raw: str) -> None:
    assert normalize_instagram_url(raw).canonical_url == CANONICAL


def test_post_and_tv_paths_supported() -> None:
    assert normalize_instagram_url("https://www.instagram.com/p/XYZ/").canonical_url == (
        "https://www.instagram.com/p/XYZ/"
    )
    assert normalize_instagram_url("https://www.instagram.com/p/XYZ/").media_type == "POST"
    assert normalize_instagram_url("https://www.instagram.com/tv/XYZ/").media_type == "REEL"


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "not-a-url",
        "https://youtube.com/watch?v=1",
        "https://www.instagram.com/stories/user/123/",   # 지원하지 않는 path
        "https://www.instagram.com/someuser/",           # 프로필 URL
        "ftp://www.instagram.com/reel/ABC/",
    ],
)
def test_invalid_urls_rejected(raw: str) -> None:
    with pytest.raises(DiscoveryError):
        normalize_instagram_url(raw)


def test_candidate_from_url_does_not_invent_data() -> None:
    """URL에 없는 정보를 추측하지 않는다."""
    candidate = candidate_from_url("https://www.instagram.com/reel/ABC123/?x=1")

    assert candidate.canonical_url == CANONICAL
    assert candidate.media_id == "ABC123"
    assert candidate.instagram_media_id is None     # 숫자 media_id는 모른다
    assert candidate.caption == "" and candidate.hashtags == []
    assert candidate.followers == 0
    assert candidate.username.startswith("unresolved:")
    assert candidate.extra["creator_unresolved"] is True


def test_candidate_from_url_uses_given_username() -> None:
    candidate = candidate_from_url(CANONICAL, username="@creator_a")
    assert candidate.username == "creator_a"
    assert candidate.extra["creator_unresolved"] is False


# --- CLI 단건 -------------------------------------------------------------
def test_add_url_stores_partial_candidate(conn) -> None:
    ingestor = CandidateIngestor(conn)
    item = ingestor.add_url("https://www.instagram.com/reel/ABC123/?utm_source=x")

    assert item.result == ADDED and item.media_pk is not None
    row = _rows(conn, "SELECT * FROM candidate_media")[0]
    assert row["canonical_url"] == CANONICAL
    assert row["instagram_media_id"] is None
    assert row["status"] == MediaStatus.NEEDS_ENRICHMENT.value   # 분석할 내용이 없음
    assert row["source"] == "manual_url"


def test_add_url_with_caption_is_ready_for_analysis(conn) -> None:
    item = CandidateIngestor(conn).add_url(CANONICAL, caption="퇴근길 #직장인")
    assert item.result == ADDED

    row = _rows(conn, "SELECT status, hashtags FROM candidate_media")[0]
    assert row["status"] == MediaStatus.NEW.value
    assert "직장인" in row["hashtags"]


def test_duplicate_url_variants_are_blocked(conn) -> None:
    ingestor = CandidateIngestor(conn)
    assert ingestor.add_url(CANONICAL).result == ADDED
    assert ingestor.add_url("https://instagram.com/reel/ABC123?utm_source=y").result == DUPLICATE
    assert len(_rows(conn, "SELECT media_pk FROM candidate_media")) == 1


def test_invalid_url_does_not_stop_others(conn) -> None:
    summary = CandidateIngestor(conn).add_urls(
        [CANONICAL, "https://youtube.com/x", "https://www.instagram.com/reel/DEF/"]
    )
    assert (summary.added, summary.invalid) == (2, 1)
    assert summary.input_count == 3


def test_import_events_recorded(conn) -> None:
    ingestor = CandidateIngestor(conn)
    ingestor.add_url(CANONICAL)
    ingestor.add_url(CANONICAL)
    ingestor.add_url("https://youtube.com/x")

    events = _rows(conn, "SELECT source, result, original_url, canonical_url FROM import_events")
    assert [e["result"] for e in events] == [ADDED, DUPLICATE, INVALID]
    assert events[0]["canonical_url"] == CANONICAL
    assert all(e["source"] == "manual_url" for e in events)


# --- Inbox CSV ------------------------------------------------------------
def _write(path, text: str, encoding: str = "utf-8") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding=encoding)


def test_inbox_csv_processed_and_moved(conn, tmp_path) -> None:
    inbox = tmp_path / "inbox"
    _write(
        inbox / "a.csv",
        "url\nhttps://www.instagram.com/reel/AAA/\nhttps://www.instagram.com/reel/BBB/\n",
    )
    summary = CandidateIngestor(conn).process_inbox(inbox)

    assert (summary.added, summary.input_count) == (2, 2)
    assert summary.files_processed == ["a.csv"]
    assert not (inbox / "a.csv").exists()            # 원본은 삭제가 아니라 이동
    assert (inbox / "processed" / "a.csv").exists()


def test_inbox_csv_utf8_sig_with_optional_columns(conn, tmp_path) -> None:
    inbox = tmp_path / "inbox"
    _write(
        inbox / "b.csv",
        "url,username,note\nhttps://www.instagram.com/reel/CCC/,creator_a,퇴근 브이로그\n",
        encoding="utf-8-sig",
    )
    summary = CandidateIngestor(conn).process_inbox(inbox)

    assert summary.added == 1
    row = _rows(conn, "SELECT c.username FROM candidate_media m JOIN creators c USING(creator_id)")[0]
    assert row["username"] == "creator_a"


def test_inbox_row_error_isolated_and_report_written(conn, tmp_path) -> None:
    inbox = tmp_path / "inbox"
    _write(
        inbox / "c.csv",
        "url\nhttps://www.instagram.com/reel/AAA/\nnot-a-url\n\nhttps://youtube.com/x\n",
    )
    summary = CandidateIngestor(conn).process_inbox(inbox)

    assert summary.added == 1 and summary.invalid == 2
    # 일부 행 실패는 파일 전체 실패가 아니다 → processed 로 이동 + Report 생성
    assert summary.files_processed == ["c.csv"] and not summary.files_failed
    report = inbox / "c_report.md"
    assert report.exists() and "not-a-url" in report.read_text(encoding="utf-8")


def test_inbox_unreadable_file_moved_to_failed(conn, tmp_path) -> None:
    inbox = tmp_path / "inbox"
    _write(inbox / "bad.csv", "id,name\n1,broken\n")
    summary = CandidateIngestor(conn).process_inbox(inbox)

    assert summary.error == 1 and summary.added == 0
    assert summary.files_failed == ["bad.csv"]
    assert (inbox / "failed" / "bad.csv").exists()   # 삭제하지 않는다


def test_same_candidate_from_cli_and_csv_is_deduped(conn, tmp_path) -> None:
    ingestor = CandidateIngestor(conn)
    assert ingestor.add_url(CANONICAL).result == ADDED

    inbox = tmp_path / "inbox"
    _write(inbox / "d.csv", "url\nhttps://www.instagram.com/reel/ABC123/?utm_source=z\n")
    summary = ingestor.process_inbox(inbox)

    assert summary.duplicate == 1 and summary.added == 0
    assert len(_rows(conn, "SELECT media_pk FROM candidate_media")) == 1


def test_existing_candidates_untouched_by_ingest(conn, sample_candidate) -> None:
    """기존 경로로 들어온 후보가 손상되지 않는다."""
    from targeting_agent.core.database import insert_candidate

    media_pk = insert_candidate(conn, sample_candidate)
    conn.commit()
    before = _rows(conn, "SELECT * FROM candidate_media WHERE media_pk = ?", media_pk)[0]

    CandidateIngestor(conn).add_url(CANONICAL)

    after = _rows(conn, "SELECT * FROM candidate_media WHERE media_pk = ?", media_pk)[0]
    assert after == before
    assert len(_rows(conn, "SELECT media_pk FROM candidate_media")) == 2


def test_missing_inbox_dir_is_noop(conn, tmp_path) -> None:
    summary = CandidateIngestor(conn).process_inbox(tmp_path / "없음")
    assert summary.input_count == 0 and not summary.has_input


def test_format_summary_lists_failures(conn) -> None:
    summary = CandidateIngestor(conn).add_urls([CANONICAL, "https://youtube.com/x"])
    text = format_summary(summary)

    assert "ADD SUMMARY" in text and "Added       1" in text
    assert "[INVALID]" in text and "youtube.com" in text
