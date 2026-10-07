"""config.yaml 로더.

모든 운영 설정값은 코드가 아니라 config.yaml에서 읽는다.
API Key/Token 등 Credential은 config.yaml이 아니라 .env(환경변수)에서만 읽는다.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

import yaml

from .exceptions import ConfigError

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = PACKAGE_ROOT / "config.yaml"
DEFAULT_ENV_PATH = PACKAGE_ROOT / ".env"

_MISSING = object()

REQUIRED_KEYS: Sequence[str] = (
    "project.name",
    "executor.mode",
    "scoring.minimum_target_score",
    "scoring.weights",
    "comments.duplicate_similarity_threshold",
    "actions.daily_limits.likes",
    "actions.daily_limits.comments",
    "actions.execution.dry_run",
)

VALID_EXECUTOR_MODES = ("manual", "official_api", "browser")


def load_dotenv(env_path: Path = DEFAULT_ENV_PATH) -> None:
    """.env 파일을 환경변수로 로드한다.

    python-dotenv가 설치되어 있으면 그것을 쓰고, 없으면 최소 파서로 대체한다
    (Dependency 최소화 원칙). 이미 설정된 환경변수는 덮어쓰지 않는다.
    """
    try:  # pragma: no cover - 설치 여부에 따른 분기
        from dotenv import load_dotenv as _load  # type: ignore

        _load(dotenv_path=env_path, override=False, encoding="utf-8")
        return
    except ImportError:
        pass

    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


@dataclass(frozen=True)
class Config:
    """config.yaml 내용을 dotted key로 읽는 얇은 래퍼."""

    raw: Mapping[str, Any]
    path: Path
    base_dir: Path = field(default=PACKAGE_ROOT)

    # --- 기본 접근자 -----------------------------------------------------
    def get(self, dotted_key: str, default: Any = _MISSING) -> Any:
        node: Any = self.raw
        for part in dotted_key.split("."):
            if not isinstance(node, Mapping) or part not in node:
                if default is _MISSING:
                    raise ConfigError(f"config.yaml에 '{dotted_key}' 설정이 없습니다.")
                return default
            node = node[part]
        return node

    def section(self, dotted_key: str) -> dict[str, Any]:
        value = self.get(dotted_key, {})
        if not isinstance(value, Mapping):
            raise ConfigError(f"config.yaml의 '{dotted_key}'는 매핑이어야 합니다.")
        return dict(value)

    def list_of(self, dotted_key: str) -> list[Any]:
        value = self.get(dotted_key, [])
        if value is None:
            return []
        if not isinstance(value, (list, tuple)):
            raise ConfigError(f"config.yaml의 '{dotted_key}'는 리스트여야 합니다.")
        return list(value)

    # --- 자주 쓰는 값 ----------------------------------------------------
    @property
    def dry_run(self) -> bool:
        return bool(self.get("actions.execution.dry_run", True))

    @property
    def executor_mode(self) -> str:
        return str(self.get("executor.mode", "manual"))

    @property
    def data_dir(self) -> Path:
        return self._resolve_path(self.get("paths.data_dir", "data"))

    @property
    def db_path(self) -> Path:
        return self._resolve_path(self.get("paths.database", "data/targeting.db"))

    @property
    def log_dir(self) -> Path:
        return self._resolve_path(self.get("paths.log_dir", "data/logs"))

    @property
    def export_dir(self) -> Path:
        return self._resolve_path(self.get("paths.export_dir", "data/exports"))

    @property
    def cache_dir(self) -> Path:
        return self._resolve_path(self.get("paths.cache_dir", "data/cache"))

    @property
    def profile_path(self) -> Path:
        return self._resolve_path(
            self.get("profile.path", "profiles/dailyreels.yaml")
        )

    def _resolve_path(self, value: Any) -> Path:
        path = Path(str(value)).expanduser()
        if not path.is_absolute():
            path = self.base_dir / path
        return path


def _validate(config: Config) -> None:
    for key in REQUIRED_KEYS:
        config.get(key)  # 없으면 ConfigError

    mode = config.executor_mode
    if mode not in VALID_EXECUTOR_MODES:
        raise ConfigError(
            f"executor.mode는 {VALID_EXECUTOR_MODES} 중 하나여야 합니다 (현재: {mode})."
        )

    weights = config.section("scoring.weights")
    if not weights:
        raise ConfigError("scoring.weights가 비어 있습니다.")
    total = sum(float(v) for v in weights.values())
    if abs(total - 1.0) > 0.01:
        raise ConfigError(
            f"scoring.weights 합계는 1.0이어야 합니다 (현재: {total:.3f})."
        )

    minimum = float(config.get("scoring.minimum_target_score"))
    auto = float(config.get("scoring.auto_action_score", minimum))
    if not 0 <= minimum <= 100 or not 0 <= auto <= 100:
        raise ConfigError("scoring 임계값은 0~100 범위여야 합니다.")

    threshold = float(config.get("comments.duplicate_similarity_threshold"))
    if not 0 < threshold <= 1:
        raise ConfigError("comments.duplicate_similarity_threshold는 0~1 사이여야 합니다.")

    for key in ("actions.daily_limits.likes", "actions.daily_limits.comments"):
        if int(config.get(key)) < 0:
            raise ConfigError(f"{key}는 0 이상이어야 합니다.")


def load_config(
    config_path: Optional[Path | str] = None,
    *,
    load_env: bool = True,
) -> Config:
    """config.yaml을 읽고 필수값을 검증한 Config를 반환한다."""
    path = Path(config_path) if config_path else DEFAULT_CONFIG_PATH
    if not path.exists():
        raise ConfigError(f"설정 파일을 찾을 수 없습니다: {path}")

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:  # pragma: no cover - 방어적 처리
        raise ConfigError(f"config.yaml 파싱 실패: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError("config.yaml 최상위는 매핑이어야 합니다.")

    if load_env:
        load_dotenv(path.parent / ".env")

    config = Config(raw=raw, path=path, base_dir=path.parent.resolve())
    _validate(config)
    return config
