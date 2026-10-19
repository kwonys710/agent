"""Import Discovery / 후보 필터 테스트."""
from __future__ import annotations

import json

import pytest

from targeting_agent.core.exceptions import DiscoveryError
from targeting_agent.core.models import RawCandidate
from targeting_agent.discovery.base import (
    dedupe_candidates,
    extract_hashtags,
    filter_by_config,
    shortcode_from_permalink,
)
from targeting_agent.discovery.import_source import ImportDiscovery, row_to_candidate

SAMPLE_CSV = (
    "media_id,permalink,username,caption,hashtags,media_type,like_count,comment_count,"
    "posted_at,followers,is_private,is_ad,language\n"
    "A1,https://www.instagram.com/reel/A1/,user_a,출근길 #직장인,직장인,REEL,100,5,2026-09-20,3000,0,0,ko\n"
    "A2,https://www.instagram.com/reel/A2/,user_b,주말 카페 #주말,주말 카페,REEL,200,9,2026-09-19,4000,0,0,ko\n"
)


def test_extract_hashtags_and_shortcode() -> None:
    assert extract_hashtags("퇴근 #직장인 #퇴근후 #직장인") == ["직장인", "퇴근후"]
    assert shortcode_from_permalink("https://www.instagram.com/reel/ABC123/") == "ABC123"
    assert shortcode_from_permalink("https://example.com/x") is None


def test_row_to_candidate_derives_media_id_from_permalink() -> None:
    candidate = row_to_candidate(
        {"permalink": "https://www.instagram.com/reel/XYZ/", "username": "@someone", "caption": "일상 #데일리"}
    )
    assert candidate.media_id == "XYZ"
    assert candidate.username == "someone"       # @ 제거
    assert candidate.hashtags == ["데일리"]       # 캡션에서 추출


def test_row_to_candidate_requires_identifier() -> None:
    with pytest.raises(DiscoveryError):
        row_to_candidate({"username": "x", "caption": "no id"})


def test_import_csv(tmp_path) -> None:
    path = tmp_path / "candidates.csv"
    path.write_text(SAMPLE_CSV, encoding="utf-8")
    candidates = ImportDiscovery(path).discover()
    assert [c.media_id for c in candidates] == ["A1", "A2"]
    assert candidates[0].followers == 3000


def test_import_json_and_txt(tmp_path) -> None:
    json_path = tmp_path / "c.json"
    json_path.write_text(
        json.dumps([{"media_id": "J1", "username": "u", "caption": "카페 #주말"}]), encoding="utf-8"
    )
    assert ImportDiscovery(json_path).discover()[0].media_id == "J1"

    txt_path = tmp_path / "c.txt"
    txt_path.write_text("https://www.instagram.com/reel/T1/\n# 주석\n", encoding="utf-8")
    assert ImportDiscovery(txt_path).discover()[0].media_id == "T1"


def test_import_missing_file(tmp_path) -> None:
    with pytest.raises(DiscoveryError):
        ImportDiscovery(tmp_path / "없음.csv").discover()


def test_dedupe_in_run() -> None:
    items = [
        RawCandidate(media_id="A", permalink="p", username="u"),
        RawCandidate(media_id="A", permalink="p", username="u"),
        RawCandidate(media_id="B", permalink="p", username="u"),
    ]
    unique, duplicates = dedupe_candidates(items)
    assert len(unique) == 2 and duplicates == 1


def test_filter_by_config_rules() -> None:
    items = [
        RawCandidate(media_id="ok", permalink="p", username="u", followers=3000, language="ko"),
        RawCandidate(media_id="priv", permalink="p", username="u", followers=3000, is_private=True),
        RawCandidate(media_id="ad", permalink="p", username="u", followers=3000, is_ad=True),
        RawCandidate(media_id="big", permalink="p", username="u", followers=500000),
        RawCandidate(media_id="en", permalink="p", username="u", followers=3000, language="en"),
        RawCandidate(media_id="photo", permalink="p", username="u", media_type="IMAGE", followers=3000),
    ]
    kept, reasons = filter_by_config(
        items,
        languages=["ko"],
        content_types=["REEL"],
        min_followers=100,
        max_followers=30000,
        skip_private=True,
        skip_ads=True,
    )
    assert [c.media_id for c in kept] == ["ok"]
    assert set(reasons) == {"private", "ad", "followers", "language", "media_type"}
