from __future__ import annotations

from pathlib import Path

import pytest

from dailyreels.config import (
    APP_DIR,
    ConfigError,
    Settings,
    load_config_file,
    load_dotenv,
    load_settings,
    resolve_root,
)


def test_resolve_root_precedence(tmp_path, monkeypatch):
    config = {"paths": {"root": str(tmp_path / "from_config")}}
    env = {"DAILYREELS_ROOT": str(tmp_path / "from_env")}

    assert resolve_root(tmp_path / "cli", config, env) == tmp_path / "cli"
    assert resolve_root(None, config, env) == tmp_path / "from_env"
    assert resolve_root(None, config, {}) == tmp_path / "from_config"
    assert resolve_root(None, {}, {}) == APP_DIR.parent


def test_load_dotenv_reads_pairs_without_clobbering_environment(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# comment\n\nDAILYREELS_ROOT=G:\\내 드라이브\\DailyReels\nEMPTY_LINE\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("DAILYREELS_ROOT", "already-set")

    loaded = load_dotenv(env_file)

    assert loaded["DAILYREELS_ROOT"] == "G:\\내 드라이브\\DailyReels"
    assert "EMPTY_LINE" not in loaded
    import os

    assert os.environ["DAILYREELS_ROOT"] == "already-set"


def test_load_settings_uses_root_and_derives_folders(tmp_path, monkeypatch):
    monkeypatch.delenv("DAILYREELS_ROOT", raising=False)
    settings = load_settings(root=tmp_path)

    assert settings.root == tmp_path.resolve()
    assert settings.data_dir == tmp_path.resolve() / "data"
    assert settings.manifest_path == tmp_path.resolve() / "data" / "manifest.json"
    assert settings.output_dir == tmp_path.resolve() / "output"
    assert ".mp4" in settings.video_extensions
    assert "output" in settings.excluded_dirs


def test_load_settings_rejects_missing_root(tmp_path):
    with pytest.raises(ConfigError, match="does not exist"):
        load_settings(root=tmp_path / "nope")


def test_shipped_config_file_parses():
    config = load_config_file(APP_DIR / "config" / "dailyreels.toml")
    assert config["scan"]["video_extensions"] == [".mp4", ".mov", ".m4v"]
    assert config["caption_style"]["time_format"] == "%H:%M"
