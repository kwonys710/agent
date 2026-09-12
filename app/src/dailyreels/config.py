from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ENV_ROOT = "DAILYREELS_ROOT"
ENV_CONFIG = "DAILYREELS_CONFIG"
ENV_FFPROBE = "DAILYREELS_FFPROBE"
ENV_FFMPEG = "DAILYREELS_FFMPEG"

DEFAULT_EXTENSIONS: tuple[str, ...] = (".mp4", ".mov", ".m4v")
DEFAULT_EXCLUDED_DIRS: tuple[str, ...] = ("app", "data", "output", "logs")
DEFAULT_TIMEZONE = "Asia/Seoul"

APP_DIR = Path(__file__).resolve().parents[2]  # .../DailyReels/app
DEFAULT_CONFIG_FILE = APP_DIR / "config" / "dailyreels.toml"


class ConfigError(RuntimeError):
    """Configuration is missing or unusable."""


@dataclass(frozen=True)
class Settings:
    root: Path
    data_dir: Path
    output_dir: Path
    logs_dir: Path
    video_extensions: tuple[str, ...] = DEFAULT_EXTENSIONS
    excluded_dirs: frozenset[str] = frozenset(DEFAULT_EXCLUDED_DIRS)
    timezone: str = DEFAULT_TIMEZONE
    ffprobe: str = "ffprobe"
    ffmpeg: str = "ffmpeg"
    render: dict[str, Any] = field(default_factory=dict)
    caption_style: dict[str, Any] = field(default_factory=dict)

    @property
    def manifest_path(self) -> Path:
        return self.data_dir / "manifest.json"


def load_dotenv(path: Path) -> dict[str, str]:
    """Minimal KEY=VALUE reader. Existing environment variables win."""
    loaded: dict[str, str] = {}
    if not path.is_file():
        return loaded
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if not key:
            continue
        loaded[key] = value
        os.environ.setdefault(key, value)
    return loaded


def load_config_file(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except (tomllib.TOMLDecodeError, OSError) as exc:
        raise ConfigError(f"could not read config file {path}: {exc}") from exc


def _resolve_dir(root: Path, value: Any, default: str) -> Path:
    candidate = Path(str(value)) if value else Path(default)
    return candidate if candidate.is_absolute() else root / candidate


def resolve_root(
    explicit: Path | str | None,
    config: dict[str, Any],
    env: dict[str, str] | None = None,
) -> Path:
    """Precedence: CLI argument > DAILYREELS_ROOT > config file > parent of app/."""
    env = os.environ if env is None else env
    for candidate in (explicit, env.get(ENV_ROOT), config.get("paths", {}).get("root")):
        if candidate:
            return Path(str(candidate)).expanduser()
    return APP_DIR.parent


def load_settings(
    root: Path | str | None = None,
    config_file: Path | str | None = None,
) -> Settings:
    load_dotenv(APP_DIR / ".env")

    config_path = Path(
        str(config_file or os.environ.get(ENV_CONFIG) or DEFAULT_CONFIG_FILE)
    )
    config = load_config_file(config_path)

    resolved_root = resolve_root(root, config)
    if not resolved_root.is_dir():
        raise ConfigError(
            f"DailyReels root does not exist: {resolved_root}\n"
            f"Set {ENV_ROOT} in app/.env or pass --root."
        )
    resolved_root = resolved_root.resolve()

    paths = config.get("paths", {})
    scan = config.get("scan", {})

    extensions = tuple(
        ext.lower() if ext.startswith(".") else f".{ext.lower()}"
        for ext in scan.get("video_extensions", DEFAULT_EXTENSIONS)
    )
    excluded = frozenset(
        str(name).lower() for name in scan.get("excluded_dirs", DEFAULT_EXCLUDED_DIRS)
    )

    return Settings(
        root=resolved_root,
        data_dir=_resolve_dir(resolved_root, paths.get("data_dir"), "data"),
        output_dir=_resolve_dir(resolved_root, paths.get("output_dir"), "output"),
        logs_dir=_resolve_dir(resolved_root, paths.get("logs_dir"), "logs"),
        video_extensions=extensions or DEFAULT_EXTENSIONS,
        excluded_dirs=excluded,
        timezone=str(scan.get("timezone", DEFAULT_TIMEZONE)),
        ffprobe=os.environ.get(ENV_FFPROBE, "ffprobe"),
        ffmpeg=os.environ.get(ENV_FFMPEG, "ffmpeg"),
        render=dict(config.get("render", {})),
        caption_style=dict(config.get("caption_style", {})),
    )
