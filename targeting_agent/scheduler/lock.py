"""Scheduled Run 중복 실행 방지 (Phase 15).

Windows Task Scheduler가 이전 실행이 끝나기 전에 다시 실행할 수 있으므로
파일 기반 lock으로 한 번에 하나만 돌게 한다. 분산 lock은 필요 없다.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from ..core.logger import get_logger

logger = get_logger("scheduler.lock")


@dataclass
class SchedulerLock:
    """실행 중 표시 파일. 오래된 lock(stale)은 넘겨받는다."""

    path: Path
    stale_minutes: int = 60
    enabled: bool = True
    _acquired: bool = False

    def acquire(self, now: Optional[datetime] = None) -> bool:
        """lock을 잡으면 True. 다른 실행이 진행 중이면 False."""
        if not self.enabled:
            return True

        now = now or datetime.now(timezone.utc)
        self.path.parent.mkdir(parents=True, exist_ok=True)

        held = self._read()
        if held is not None:
            if self._is_stale(held, now):
                logger.warning(
                    "오래된 lock을 회수합니다(생성 %s, TTL %d분).", held.get("created_at"), self.stale_minutes
                )
            else:
                logger.info("다른 Scheduled Run이 진행 중입니다(pid=%s).", held.get("pid"))
                return False

        self.path.write_text(
            json.dumps(
                {"pid": os.getpid(), "created_at": now.isoformat(timespec="seconds")},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        self._acquired = True
        return True

    def release(self) -> None:
        """정상·비정상 종료 모두에서 호출된다."""
        if not self.enabled or not self._acquired:
            return
        try:
            self.path.unlink(missing_ok=True)
        except OSError:  # pragma: no cover - 파일 잠김 등
            logger.warning("lock 파일을 지우지 못했습니다: %s", self.path)
        self._acquired = False

    # --- 내부 ------------------------------------------------------------
    def _read(self) -> Optional[dict]:
        if not self.path.exists():
            return None
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {"pid": None, "created_at": None}  # 깨진 lock은 stale로 취급
        return data if isinstance(data, dict) else {}

    def _is_stale(self, held: dict, now: datetime) -> bool:
        raw = held.get("created_at")
        if not raw:
            return True
        try:
            created = datetime.fromisoformat(str(raw))
        except ValueError:
            return True
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        return now - created >= timedelta(minutes=max(self.stale_minutes, 1))

    def __enter__(self) -> "SchedulerLock":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.release()
