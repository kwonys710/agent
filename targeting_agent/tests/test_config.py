"""config 로딩/검증 테스트."""
from __future__ import annotations

import pytest
import yaml

from targeting_agent.core.config import load_config
from targeting_agent.core.exceptions import ConfigError


def test_load_config_basic(config) -> None:
    assert config.get("project.name")
    assert config.dry_run is True          # 기본은 반드시 Dry Run
    assert config.executor_mode == "manual"  # 기본은 반드시 Manual
    assert config.db_path.name == "targeting.db"


def test_missing_file() -> None:
    with pytest.raises(ConfigError):
        load_config("없는파일.yaml", load_env=False)


def test_weight_sum_validation(tmp_path, config) -> None:
    raw = dict(config.raw)
    raw["scoring"] = {
        **raw["scoring"],
        "weights": {"content_similarity": 0.9, "creator_fit": 0.9},
    }
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    with pytest.raises(ConfigError, match="weights"):
        load_config(path, load_env=False)


def test_invalid_executor_mode(tmp_path, config) -> None:
    raw = dict(config.raw)
    raw["executor"] = {**raw["executor"], "mode": "telepathy"}
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    with pytest.raises(ConfigError, match="executor.mode"):
        load_config(path, load_env=False)


def test_dotted_get_default(config) -> None:
    assert config.get("없는.키", "기본값") == "기본값"
    with pytest.raises(ConfigError):
        config.get("없는.키")
