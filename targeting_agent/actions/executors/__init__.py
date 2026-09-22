"""Executor 구현 모음과 팩토리."""
from __future__ import annotations

from pathlib import Path

from ...core.exceptions import ConfigError
from ...core.models import ExecutionResult  # re-export 편의
from .base import BaseExecutor
from .browser import BrowserExecutor
from .manual import ManualExecutor
from .official_api import OfficialAPIExecutor

__all__ = [
    "BaseExecutor",
    "BrowserExecutor",
    "ManualExecutor",
    "OfficialAPIExecutor",
    "ExecutionResult",
    "create_executor",
]


def create_executor(
    mode: str, *, export_dir: Path | str, run_id: str, dry_run: bool
) -> BaseExecutor:
    """config.executor.mode에 맞는 Executor를 생성한다."""
    normalized = (mode or "manual").lower()
    if normalized == "manual":
        return ManualExecutor(export_dir=export_dir, run_id=run_id, dry_run=dry_run)
    if normalized == "official_api":
        return OfficialAPIExecutor(dry_run=dry_run)
    if normalized == "browser":
        return BrowserExecutor(dry_run=dry_run)
    raise ConfigError(f"알 수 없는 executor.mode: {mode}")
