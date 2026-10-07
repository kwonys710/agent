"""게시물 공개 메타태그 파서 테스트 (Phase 18A.6).

실제 Instagram에 접속하지 않는다 — 실기에서 관찰된 **문자열 형태**만 검증한다.
"""
from __future__ import annotations

import pytest

from targeting_agent.discovery.browser_post_meta import parse_post_meta

# 실기에서 그대로 관찰된 형태(2026-10-07).
REAL_DESC = (
    '9,367 likes, 62 comments - cle_.mence - August 23, 2026: '
    '"네시 반에 일어나서 6시까지 출근하고 12시간 일하고 새벽 한시까지 회식하는 현실 직장인 브이로그".'
)


def test_작성자와_본문과_반응수를_읽는다():
    meta = parse_post_meta(og_description=REAL_DESC)

    assert meta.username == "cle_.mence"
    assert meta.caption.startswith("네시 반에 일어나서")
    assert meta.caption.endswith("브이로그")  # 닫는 따옴표와 마침표는 본문이 아니다
    assert meta.like_count == 9367
    assert meta.comment_count == 62


def test_반올림된_반응수는_숫자로_만들지_않는다():
    """'15K likes'는 반올림값이다 — 정확하지 않은 수를 지어내지 않는다."""
    meta = parse_post_meta(
        og_description='15K likes, 669 comments - liam_mmgz - November 7, 2025: "회사 이야기 #직장인".'
    )

    assert meta.username == "liam_mmgz"
    assert meta.like_count is None
    assert meta.comment_count == 669


def test_본문이_잘려_있어도_그대로_둔다():
    """og:description은 길이가 잘린다. 없는 글자를 채워 넣지 않는다."""
    meta = parse_post_meta(
        og_description='9,367 likes, 62 comments - cle_.mence - August 23, 2026: "앞부분만 남고 뒤가 잘린 본'
    )

    assert meta.caption == "앞부분만 남고 뒤가 잘린 본"


def test_description이_없으면_title로_본문을_읽는다():
    meta = parse_post_meta(og_title='Instagram의 민대리님 : "본문입니다 #직장인"')

    assert meta.caption == "본문입니다 #직장인"
    assert meta.username == ""  # title에는 username이 없다 — 추측하지 않는다


@pytest.mark.parametrize("desc, title", [("", ""), ("설명이 없는 문자열", ""), ("  ", "  ")])
def test_읽을_게_없으면_비워_둔다(desc: str, title: str):
    meta = parse_post_meta(og_description=desc, og_title=title)

    assert meta.empty
    assert meta.username == "" and meta.caption == ""
    assert meta.like_count is None and meta.comment_count is None
