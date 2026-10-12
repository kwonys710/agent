"""내부 Rate Limiter.

플랫폼 제한을 우회하기 위한 장치가 아니라, 운영자가 정한 **보수적인 내부 한도**를
Action 실행 전에 강제하는 장치다.

검사 항목:
- 오늘 실행한 LIKE / COMMENT 수 (config.actions.daily_limits)
- 동일 Creator 오늘 Interaction 수 (per_creator.max_actions_per_day)
- 동일 Creator cooldown (per_creator.cooldown_days)
- 동일 media 중복 Interaction 여부
- 이번 실행에서의 최대 Action 수 (execution.max_actions_per_run)

dry_run 실행과 실제 실행의 카운트는 분리 집계한다.
(Dry Run이 실제 운영 한도를 소모하지 않도록)
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from ..core.config import Config
from ..core.database import (
    count_actions_today,
    count_awaiting_today,
    count_creator_actions_today,
    count_creator_media_today,
    has_interaction,
    last_creator_interaction_at,
    today_str,
)
from ..core.models import ActionType


@dataclass(frozen=True)
class LimitDecision:
    allowed: bool
    reason: str = ""

    def __bool__(self) -> bool:  # pragma: no cover - 편의 메서드
        return self.allowed


class RateLimiter:
    def __init__(self, config: Config, conn: sqlite3.Connection, dry_run: Optional[bool] = None) -> None:
        self.conn = conn
        self.dry_run = config.dry_run if dry_run is None else dry_run
        self.tz_offset = int(config.get("actions.daily_limits.timezone_offset_hours", 9))
        self.daily_limits = {
            ActionType.LIKE: int(config.get("actions.daily_limits.likes", 0)),
            ActionType.COMMENT: int(config.get("actions.daily_limits.comments", 0)),
        }
        self.max_per_creator_day = int(config.get("actions.per_creator.max_actions_per_day", 1))
        # media: 같은 게시물에 대한 LIKE+COMMENT를 1건으로 센다(기본)
        # action: LIKE/COMMENT를 각각 1건으로 센다(더 보수적)
        self.creator_count_mode = str(config.get("actions.per_creator.count_mode", "media"))
        self.cooldown_days = int(config.get("actions.per_creator.cooldown_days", 0))
        self.max_actions_per_run = int(config.get("actions.execution.max_actions_per_run", 0))
        self.skip_if_already_interacted = bool(config.get("safety.skip_if_already_interacted", True))
        self.executed_this_run = 0
        self._date = today_str(self.tz_offset)

    # --- 조회 ------------------------------------------------------------
    def used_today(self, action_type: ActionType) -> int:
        """오늘 사용량 = 기록된 Interaction + 운영자 확인 대기 중인 Action."""
        done = count_actions_today(
            self.conn, action_type, self._date, self.dry_run, self.tz_offset
        )
        awaiting = count_awaiting_today(
            self.conn, action_type, self._date, self.dry_run, self.tz_offset
        )
        return done + awaiting

    def remaining(self, action_type: ActionType) -> int:
        return max(0, self.daily_limits.get(action_type, 0) - self.used_today(action_type))

    def usage_summary(self) -> dict[str, tuple[int, int]]:
        """{'LIKE': (사용, 한도), 'COMMENT': (사용, 한도)}"""
        return {
            action_type.value: (self.used_today(action_type), self.daily_limits.get(action_type, 0))
            for action_type in (ActionType.LIKE, ActionType.COMMENT)
        }

    # --- 판정 ------------------------------------------------------------
    def check(self, action_type: ActionType, creator_id: int, media_pk: int) -> LimitDecision:
        """Action 실행 가능 여부를 판정한다."""
        if self.max_actions_per_run and self.executed_this_run >= self.max_actions_per_run:
            return LimitDecision(False, f"max_actions_per_run({self.max_actions_per_run})")

        limit = self.daily_limits.get(action_type, 0)
        used = self.used_today(action_type)
        if used >= limit:
            return LimitDecision(False, f"daily_limit_{action_type.value.lower()}({used}/{limit})")

        if self.skip_if_already_interacted and has_interaction(
            self.conn, media_pk, self.dry_run, action_type
        ):
            return LimitDecision(False, "already_interacted_media")

        # 같은 게시물에 대한 후속 Action(LIKE 후 COMMENT)은 새로운 Creator 접촉이 아니므로
        # Creator 일일 한도/cooldown 검사를 건너뛴다(일일 총량 검사는 위에서 이미 수행).
        same_post_followup = (
            self.creator_count_mode == "media"
            and has_interaction(self.conn, media_pk, self.dry_run)
        )
        if same_post_followup:
            return LimitDecision(True)

        counter = (
            count_creator_media_today
            if self.creator_count_mode == "media"
            else count_creator_actions_today
        )
        creator_used = counter(
            self.conn, creator_id, self._date, self.dry_run, self.tz_offset
        )
        if self.max_per_creator_day and creator_used >= self.max_per_creator_day:
            return LimitDecision(
                False, f"creator_daily_limit({creator_used}/{self.max_per_creator_day})"
            )

        if self.cooldown_days:
            last = last_creator_interaction_at(self.conn, creator_id, self.dry_run)
            if last:
                try:
                    last_dt = datetime.fromisoformat(last)
                except ValueError:  # pragma: no cover - 형식 이상 데이터 방어
                    last_dt = None
                if last_dt is not None:
                    if last_dt.tzinfo is None:
                        last_dt = last_dt.replace(tzinfo=timezone.utc)
                    days = (datetime.now(timezone.utc) - last_dt).total_seconds() / 86400
                    if days < self.cooldown_days:
                        return LimitDecision(
                            False, f"creator_cooldown({days:.1f}/{self.cooldown_days}d)"
                        )

        return LimitDecision(True)

    def record_executed(self) -> None:
        """실행 성공 시 이번 실행 카운터를 증가시킨다(일일 카운트는 DB 기준)."""
        self.executed_this_run += 1
