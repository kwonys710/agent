"""Targeting Agent 테스트 공통 fixture."""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = PACKAGE_ROOT.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from targeting_agent.analysis.profile_analyzer import TargetProfile, load_profile  # noqa: E402
from targeting_agent.core.config import Config, load_config  # noqa: E402
from targeting_agent.core.database import get_connection, init_db  # noqa: E402
from targeting_agent.core.models import RawCandidate  # noqa: E402


@pytest.fixture()
def config() -> Config:
    """테스트용 설정. 실제 Claude CLI를 호출하지 않도록 ai.provider를 내린다."""
    loaded = load_config(PACKAGE_ROOT / "config.yaml", load_env=False)
    raw = {**loaded.raw, "ai": {**loaded.raw.get("ai", {}), "provider": "heuristic"}}
    return Config(raw=raw, path=loaded.path, base_dir=loaded.base_dir)


@pytest.fixture()
def profile() -> TargetProfile:
    return load_profile(PACKAGE_ROOT / "profiles" / "dailyreels.yaml")


@pytest.fixture()
def conn() -> sqlite3.Connection:
    connection = get_connection(":memory:")
    init_db(connection)
    yield connection
    connection.close()


@pytest.fixture()
def sample_candidate() -> RawCandidate:
    return RawCandidate(
        media_id="TEST001",
        permalink="https://www.instagram.com/reel/TEST001/",
        username="office_daily_kim",
        caption="퇴근 후 저녁 만들어 먹기 #직장인 #퇴근후 #일상브이로그",
        hashtags=["직장인", "퇴근후", "일상브이로그"],
        like_count=400,
        comment_count=20,
        posted_at="2026-09-20",
        followers=5000,
        language="ko",
    )
