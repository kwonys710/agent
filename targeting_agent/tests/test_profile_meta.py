"""프로필 메타(팔로워) 읽기 테스트 (Phase 19N).

Instagram에 접속하지 않는다. 파서는 문자열만, 보강 패스는 Fake로 검증한다.
"""
from __future__ import annotations

import logging
import sqlite3

import pytest

from targeting_agent.analysis.profile_analyzer import creator_fit
from targeting_agent.core.config import Config
from targeting_agent.discovery.browser_instagram import InstagramBrowserDiscovery
from targeting_agent.discovery.browser_models import PostDetail
from targeting_agent.discovery.browser_profile_meta import (
    ProfileMeta,
    parse_profile_meta,
    profile_url,
)

from .test_browser_discovery import FakeBrowser

DESC_EXACT = (
    "1,234 Followers, 567 Following, 89 Posts - "
    "See Instagram photos and videos from 민대리 (@liam_mmgz)"
)
DESC_ROUNDED = (
    "15K Followers, 300 Following, 1,200 Posts - "
    "See Instagram photos and videos from X (@office_daily_kim)"
)


# --- 파서 ------------------------------------------------------------------
def test_정확한_팔로워_수를_읽는다():
    meta = parse_profile_meta(DESC_EXACT)

    assert meta.followers == 1234
    assert meta.approximate is False
    assert meta.username == "liam_mmgz"
    assert meta.known is True


def test_반올림값은_표시해서_받는다():
    """creator_fit은 구간 함수라 규모만 쓴다 — 플랫폼 반올림값도 받되 표시한다."""
    meta = parse_profile_meta(DESC_ROUNDED)

    assert meta.followers == 15000
    assert meta.approximate is True


@pytest.mark.parametrize(
    "desc, expected",
    [
        ("1.2M followers, 10 following", 1_200_000),
        ("500 followers", 500),
        ("0 followers", 0),
    ],
)
def test_단위를_해석한다(desc: str, expected: int):
    assert parse_profile_meta(desc).followers == expected


def test_읽지_못하면_none이다():
    """0으로 단정하지 않는다 — 모르는 것과 0은 다르다."""
    assert parse_profile_meta("").followers is None
    assert parse_profile_meta("See Instagram photos and videos").followers is None
    assert parse_profile_meta("").known is False


def test_비공개_계정을_알아본다():
    assert parse_profile_meta("This account is private").is_private is True
    assert parse_profile_meta("", body_text="비공개 계정입니다").is_private is True
    assert parse_profile_meta(DESC_EXACT).is_private is False


def test_프로필_주소는_표준_경로다():
    assert profile_url("office_daily_kim", base_url="https://www.instagram.com") == (
        "https://www.instagram.com/office_daily_kim/"
    )
    assert profile_url("@user", base_url="https://www.instagram.com").endswith("/user/")
    assert profile_url("", base_url="https://www.instagram.com") == ""


# --- 보강 패스 -------------------------------------------------------------
class ProfileBrowser(FakeBrowser):
    """read_profile을 구현한 Fake. 몇 번 열렸는지 센다."""

    def __init__(self, metas: dict[str, ProfileMeta], **kwargs) -> None:
        super().__init__(**kwargs)
        self.metas = metas
        self.opened: list[str] = []

    def read_profile(self, username: str):
        self.opened.append(username)
        return self.metas.get(username)


def browser_with_creators(pairs: list[tuple[str, str]], metas: dict) -> ProfileBrowser:
    """(코드, username) 쌍으로 후보를 만드는 Fake."""
    links = {"직장인": [f"https://www.instagram.com/{user}/reel/{code}/" for code, user in pairs]}
    details = {
        f"https://www.instagram.com/reel/{code}/": PostDetail(
            permalink=f"https://www.instagram.com/reel/{code}/",
            username=user,
            caption="퇴근 후 저녁 #직장인 #퇴근",
            media_type="REEL",
        )
        for code, user in pairs
    }
    return ProfileBrowser(metas, links=links, details=details)


def test_creator마다_한_번만_프로필을_연다():
    pairs = [("AAA111", "creator_a"), ("AAA222", "creator_a"), ("BBB111", "creator_b")]
    metas = {
        "creator_a": ProfileMeta(username="creator_a", followers=5000),
        "creator_b": ProfileMeta(username="creator_b", followers=8000),
    }
    browser = browser_with_creators(pairs, metas)
    discovery = InstagramBrowserDiscovery(browser, ["직장인"], max_candidates_per_query=10)

    candidates = discovery.discover()

    assert browser.opened == ["creator_a", "creator_b"]  # 후보 3건이지만 2번만 연다
    assert discovery.profile_attempts == 2
    assert discovery.profile_followers_found == 2
    assert {c.followers for c in candidates} == {5000, 8000}


def test_상한을_넘겨_열지_않는다():
    pairs = [(f"C{i:03d}", f"creator_{i}") for i in range(5)]
    metas = {f"creator_{i}": ProfileMeta(followers=5000) for i in range(5)}
    browser = browser_with_creators(pairs, metas)
    discovery = InstagramBrowserDiscovery(
        browser, ["직장인"], max_candidates_per_query=10, max_profiles=2
    )

    discovery.discover()

    assert len(browser.opened) == 2


def test_상한이_0이면_아예_열지_않는다():
    pairs = [("AAA111", "creator_a")]
    browser = browser_with_creators(pairs, {"creator_a": ProfileMeta(followers=5000)})
    discovery = InstagramBrowserDiscovery(
        browser, ["직장인"], max_candidates_per_query=10, max_profiles=0
    )

    candidates = discovery.discover()

    assert browser.opened == []
    assert candidates[0].followers == 0


def test_읽지_못하면_0으로_남긴다():
    pairs = [("AAA111", "creator_a")]
    browser = browser_with_creators(pairs, {})  # 프로필 메타 없음
    discovery = InstagramBrowserDiscovery(browser, ["직장인"], max_candidates_per_query=10)

    candidates = discovery.discover()

    assert browser.opened == ["creator_a"]
    assert candidates[0].followers == 0  # 지어내지 않는다


def test_비공개_계정은_표시된다():
    pairs = [("AAA111", "creator_a")]
    metas = {"creator_a": ProfileMeta(followers=None, is_private=True)}
    browser = browser_with_creators(pairs, metas)
    discovery = InstagramBrowserDiscovery(browser, ["직장인"], max_candidates_per_query=10)

    candidates = discovery.discover()

    assert candidates[0].is_private is True


def test_read_profile이_없는_브라우저도_동작한다():
    """선택 기능이다 — 구현하지 않은 브라우저로도 Discovery는 돌아간다."""
    links = {"직장인": ["https://www.instagram.com/creator_a/reel/AAA111/"]}
    details = {
        "https://www.instagram.com/reel/AAA111/": PostDetail(
            permalink="https://www.instagram.com/reel/AAA111/",
            username="creator_a",
            caption="퇴근 후 #직장인",
            media_type="REEL",
        )
    }
    discovery = InstagramBrowserDiscovery(
        FakeBrowser(links=links, details=details), ["직장인"], max_candidates_per_query=10
    )

    assert len(discovery.discover()) == 1
    assert discovery.profile_attempts == 0


def test_프로필_실패가_run을_멈추지_않는다():
    pairs = [("AAA111", "creator_a")]
    browser = browser_with_creators(pairs, {})

    def boom(username: str):
        raise RuntimeError("profile load failed")

    browser.read_profile = boom  # type: ignore[assignment]
    discovery = InstagramBrowserDiscovery(browser, ["직장인"], max_candidates_per_query=10)

    candidates = discovery.discover()

    assert len(candidates) == 1  # 후보는 그대로 남는다


# --- 전멸 관측성(P1-2) ------------------------------------------------------
def _run_enrich(metas: dict, names: list[str], *, boom: set[str] | None = None, caplog=None):
    """Creator마다 한 후보씩 만들어 discover를 돌린다. boom에 든 이름은 reader가 예외를 던진다."""
    pairs = [(f"C{i:03d}", name) for i, name in enumerate(names)]
    browser = browser_with_creators(pairs, metas)
    if boom:
        original = browser.read_profile

        def reader(username: str):
            if username in boom:
                raise RuntimeError("profile load failed")
            return original(username)

        browser.read_profile = reader  # type: ignore[assignment]
    discovery = InstagramBrowserDiscovery(browser, ["직장인"], max_candidates_per_query=10)
    return discovery, discovery.discover()


def _enrich_warnings(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING and "프로필 보강" in r.getMessage()]


def test_비공개_아닌_3명_전원_파싱실패면_경고한다(caplog):
    names = ["a", "b", "c"]
    metas = {n: ProfileMeta(username=n, followers=None) for n in names}
    with caplog.at_level(logging.WARNING):
        discovery, _ = _run_enrich(metas, names)

    warnings = _enrich_warnings(caplog)
    assert len(warnings) == 1
    assert "전멸" in warnings[0]
    assert "시도 3" in warnings[0]  # 분모 포함
    assert "og:description" in warnings[0]
    assert discovery.profile_parse_failed == 3


def test_비공개_3명만으로_0이면_경고하지_않는다(caplog):
    names = ["a", "b", "c"]
    metas = {n: ProfileMeta(username=n, followers=None, is_private=True) for n in names}
    with caplog.at_level(logging.WARNING):
        discovery, _ = _run_enrich(metas, names)

    assert _enrich_warnings(caplog) == []
    assert discovery.profile_private == 3
    assert discovery.profile_parse_failed == 0


def test_표본_2명_전원_실패는_경고하지_않는다(caplog):
    names = ["a", "b"]
    metas = {n: ProfileMeta(username=n, followers=None) for n in names}
    with caplog.at_level(logging.WARNING):
        discovery, _ = _run_enrich(metas, names)

    assert _enrich_warnings(caplog) == []
    assert discovery.profile_parse_failed == 2


def test_팔로워를_한_건이라도_확보하면_경고하지_않는다(caplog):
    names = ["a", "b", "c", "d"]
    metas = {
        "a": ProfileMeta(username="a", followers=5000),
        "b": ProfileMeta(username="b", followers=None),
        "c": ProfileMeta(username="c", followers=None),
        "d": ProfileMeta(username="d", followers=None),
    }
    with caplog.at_level(logging.WARNING):
        discovery, _ = _run_enrich(metas, names)

    assert _enrich_warnings(caplog) == []
    assert discovery.profile_followers_found == 1


def test_사유별_카운터_합이_시도_수와_같다(caplog):
    names = ["ok", "priv", "nofollow", "unopened", "crash"]
    metas = {
        "ok": ProfileMeta(username="ok", followers=5000),
        "priv": ProfileMeta(username="priv", followers=None, is_private=True),
        "nofollow": ProfileMeta(username="nofollow", followers=None),
        # unopened는 metas에 없어 reader가 None을 돌려준다
    }
    with caplog.at_level(logging.WARNING):
        discovery, _ = _run_enrich(metas, names, boom={"crash"})

    assert discovery.profile_attempts == 5
    assert discovery.profile_followers_found == 1
    assert discovery.profile_private == 1
    assert discovery.profile_parse_failed == 1
    assert discovery.profile_unopened == 1
    assert discovery.profile_errors == 1
    assert discovery.profile_accounted() is True
    assert not any("집계 불일치" in m for m in _enrich_warnings(caplog))


def test_집계가_어긋나면_로그로_드러난다(caplog):
    names = ["a"]
    metas = {"a": ProfileMeta(username="a", followers=1000)}
    discovery, candidates = _run_enrich(metas, names)
    assert discovery.profile_accounted() is True

    discovery.profile_attempts += 1  # 설명되지 않는 시도를 강제로 만든다
    assert discovery.profile_accounted() is False
    with caplog.at_level(logging.WARNING):
        discovery._enrich_followers(candidates)  # 예외 없이 WARNING으로만 드러난다

    assert any("집계 불일치" in m for m in _enrich_warnings(caplog))


def test_reader_예외는_격리되고_나머지는_계속_처리된다():
    names = ["crash", "a"]
    metas = {"a": ProfileMeta(username="a", followers=7000)}
    discovery, candidates = _run_enrich(metas, names, boom={"crash"})

    assert len(candidates) == 2  # 후보는 그대로 남는다
    assert discovery.profile_errors == 1
    assert discovery.profile_followers_found == 1
    assert {c.followers for c in candidates} == {0, 7000}


# --- 점수 효과 -------------------------------------------------------------
def test_팔로워를_채우면_creator_fit이_중립값을_벗어난다(config: Config):
    low = int(config.get("targeting.min_followers", 1000))
    high = int(config.get("targeting.max_followers", 50000))

    unknown = creator_fit(0, min_followers=low, max_followers=high, is_private=False)
    known = creator_fit(
        (low + high) // 2, min_followers=low, max_followers=high, is_private=False
    )

    assert unknown == 0.5  # 정보 없음 = 중립값
    assert known > unknown  # 채우면 점수가 올라간다
