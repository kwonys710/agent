"""Scheduler ↔ Query Planner 연결 (Phase 19J).

Scheduler는 "언제 돌릴지"만 담당한다. "무엇을 검색할지"는 Query Planner가 정한다.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from targeting_agent.core.config import Config
from targeting_agent.discovery.browser_models import PostDetail
from targeting_agent.discovery.browser_runner import run_browser_discovery
from targeting_agent.discovery.query_bank import get_query, seed_bank

from .test_browser_discovery import FakeBrowser, FakeProcessor

PACKAGE_ROOT = Path(__file__).resolve().parents[1]


def planner_config(config: Config, **overrides) -> Config:
    raw = {
        **config.raw,
        "browser_discovery": {
            "enabled": True,
            "headless": True,
            "queries": [],
            "media_types": ["REEL"],
            "limits": {
                "max_queries_per_run": 10,
                "max_candidates_per_query": 2,
                "max_candidates_per_run": 20,
                "max_analyze_per_run": 10,
            },
            "lock": {"enabled": False},
            "debug": {"enabled": False},
        },
        "query_planner": {**config.raw["query_planner"], "max_queries_per_run": 4, **overrides},
    }
    return Config(raw=raw, path=config.path, base_dir=config.base_dir)


def any_query_browser() -> FakeBrowser:
    """어떤 검색어로 와도 후보 1건을 돌려주는 Fake(검색어 선택을 보기 위한 것)."""

    class AnyQueryBrowser(FakeBrowser):
        def collect_post_links(self, limit: int) -> list[str]:
            query = next(c[1] for c in reversed(self.calls) if c[0] == "search")
            self.calls.append(("collect", query, limit))
            code = f"C{abs(hash(query)) % 100000:05d}"
            url = f"https://www.instagram.com/creator_{code.lower()}/reel/{code}/"
            canonical = f"https://www.instagram.com/reel/{code}/"
            self.details[canonical] = PostDetail(
                permalink=canonical,
                username=f"creator_{code.lower()}",
                caption=f"{query} 관련 영상 #직장인 #퇴근",
                media_type="REEL",
            )
            return [url]

    return AnyQueryBrowser(links={}, details={})


def test_scheduler는_검색어를_직접_고르지_않는다():
    """Scheduler 모듈에 검색어 목록이 하드코딩돼 있지 않아야 한다."""
    source = (PACKAGE_ROOT / "scheduler" / "runner.py").read_text(encoding="utf-8")

    assert "discovery.hashtags" not in source
    assert "browser_discovery.queries" not in source
    assert "query_bank" not in source  # 선택은 Planner의 일이다


def test_planner가_검색어를_정한다(config: Config, conn: sqlite3.Connection):
    cfg = planner_config(config)

    result = run_browser_discovery(
        cfg, conn, browser=any_query_browser(), processor_factory=lambda c, k: FakeProcessor(k)
    )

    assert result.query_source == "planner"
    assert len(result.planned_queries) == 4
    assert result.added >= 1


def test_연속_run은_다른_검색어_조합을_쓴다(config: Config, conn: sqlite3.Connection):
    cfg = planner_config(config)

    first = run_browser_discovery(
        cfg, conn, browser=any_query_browser(), processor_factory=lambda c, k: FakeProcessor(k)
    )
    second = run_browser_discovery(
        cfg, conn, browser=any_query_browser(), processor_factory=lambda c, k: FakeProcessor(k)
    )

    assert set(first.planned_queries) != set(second.planned_queries)


def test_run_결과가_검색어_성과로_누적된다(config: Config, conn: sqlite3.Connection):
    cfg = planner_config(config)

    result = run_browser_discovery(
        cfg, conn, browser=any_query_browser(), processor_factory=lambda c, k: FakeProcessor(k)
    )

    recorded = [get_query(conn, query) for query in result.planned_queries]
    assert all(record is not None for record in recorded)
    assert sum(record.run_count for record in recorded) >= 1
    assert sum(record.new_count for record in recorded) >= 1


def test_명시적_query는_planner를_덮어쓴다(config: Config, conn: sqlite3.Connection):
    cfg = planner_config(config)

    result = run_browser_discovery(
        cfg,
        conn,
        browser=any_query_browser(),
        queries=["직장인"],
        process=False,
    )

    assert result.query_source == "explicit"
    assert result.planned_queries == ["직장인"]
    # 운영자가 직접 준 검색어도 은행에 반영된다(성과 추적을 위해).
    assert get_query(conn, "직장인") is not None


def test_config_해시태그도_은행에_반영된다(config: Config, conn: sqlite3.Connection):
    """기존 discovery.hashtags 설정이 유실되지 않고 은행으로 들어온다."""
    cfg = planner_config(config, seed_on_start=False)

    run_browser_discovery(cfg, conn, browser=any_query_browser(), process=False)

    configured = [str(h) for h in config.get("discovery.hashtags", [])]
    assert configured, "config에 해시태그가 있어야 이 테스트가 의미 있다"
    assert all(get_query(conn, query) is not None for query in configured)


def test_planner를_끄면_config_목록을_쓴다(config: Config, conn: sqlite3.Connection):
    cfg = planner_config(config)
    raw = {
        **cfg.raw,
        "query_planner": {**cfg.raw["query_planner"], "enabled": False},
        "browser_discovery": {**cfg.raw["browser_discovery"], "queries": ["퇴근"]},
    }
    off = Config(raw=raw, path=cfg.path, base_dir=cfg.base_dir)

    result = run_browser_discovery(off, conn, browser=any_query_browser(), process=False)

    assert result.query_source == "config"
    assert result.planned_queries == ["퇴근"]


def test_은행이_비어_있고_config도_없으면_실패로_남긴다(config: Config, conn: sqlite3.Connection):
    """seed도 끄고 config 검색어(discovery.hashtags 포함)도 비우면 실행할 것이 없다."""
    cfg = planner_config(config, seed_on_start=False)
    raw = {**cfg.raw, "discovery": {**cfg.raw["discovery"], "hashtags": []}}
    empty = Config(raw=raw, path=cfg.path, base_dir=cfg.base_dir)

    result = run_browser_discovery(empty, conn, browser=any_query_browser(), process=False)

    assert result.status == "FAILED"
    assert "검색어가 없습니다" in result.message
