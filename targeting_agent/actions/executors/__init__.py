"""Executor 구현 모음과 팩토리."""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Optional

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
    mode: str,
    *,
    export_dir: Path | str,
    run_id: str,
    dry_run: bool,
    config: Optional[Any] = None,
    conn: Optional[sqlite3.Connection] = None,
    page: Optional[Any] = None,
) -> BaseExecutor:
    """config.executor.mode에 맞는 Executor를 생성한다.

    browser Executor는 config(mode/limit)와 conn(중복 검사)이 필요하다. 주지 않으면
    DRY_RUN으로 동작한다 — 실수로 LIVE가 켜지지 않게 하는 쪽을 기본값으로 둔다.
    """
    normalized = (mode or "manual").lower()
    if normalized == "manual":
        return ManualExecutor(export_dir=export_dir, run_id=run_id, dry_run=dry_run)
    if normalized == "official_api":
        return OfficialAPIExecutor(dry_run=dry_run)
    if normalized == "browser":
        return BrowserExecutor(dry_run=dry_run, config=config, conn=conn, page=page)
    raise ConfigError(f"알 수 없는 executor.mode: {mode}")
