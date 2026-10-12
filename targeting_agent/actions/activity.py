"""Activity Controller — 계정 건강 기반 활동 상태 (Phase 19H).

목표는 "플랫폼 경고 없이, 품질 기준을 만족하는 고유 후보에 대해,
**운영자가 설정한 상한 안에서** 유효 Activity를 수행"하는 것이다.

하지 않는 것:
- 경고가 없다고 활동량을 **자동으로 늘리지 않는다**. 상한은 운영자가 정한 값이 천장이다.
- 탐지 한계를 탐색하지 않는다. 자동 조절은 **감소 방향으로만** 동작한다.

상태:
    NORMAL   설정된 상한 그대로
    CAUTION  경고 감지 → 상한을 비율로 줄인다
    STOPPED  Challenge / Action Block / 로그인 필요 → 실행하지 않는다
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Optional

from ..core.config import Config
from ..core.database import get_state, set_state, utc_now
from ..core.logger import get_logger
from ..core.models import ActionType

logger = get_logger("actions.activity")

STATE_KEY = "activity_state"
STATE_REASON_KEY = "activity_state_reason"
STATE_SINCE_KEY = "activity_state_since"


class ActivityState(str, Enum):
    NORMAL = "NORMAL"
    CAUTION = "CAUTION"
    STOPPED = "STOPPED"

    @property
    def can_act(self) -> bool:
        return self is not ActivityState.STOPPED

    @property
    def severity(self) -> int:
        return {"NORMAL": 0, "CAUTION": 1, "STOPPED": 2}[self.value]


# 중단으로 올리는 신호(= 즉시 STOPPED)
STOP_SIGNALS = frozenset({"ACTION_BLOCKED", "CHALLENGE", "LOGIN_REQUIRED"})
# 주의로 올리는 신호(= CAUTION)
CAUTION_SIGNALS = frozenset({"PLATFORM_WARNING", "TRY_AGAIN_LATER", "UNKNOWN"})


@dataclass
class ActivityStatus:
    """현재 활동 상태와 그에 따른 상한."""

    state: ActivityState = ActivityState.NORMAL
    reason: str = ""
    since: Optional[str] = None
    limits: dict[str, int] = field(default_factory=dict)
    configured_limits: dict[str, int] = field(default_factory=dict)

    @property
    def can_act(self) -> bool:
        return self.state.can_act

    def limit_for(self, action_type: ActionType) -> int:
        return int(self.limits.get(action_type.value, 0))

    def as_rows(self) -> dict[str, str]:
        return {
            "상태": self.state.value,
            "사유": self.reason or "-",
            "변경 시각": (self.since or "-")[:19],
            "적용 상한": ", ".join(f"{k} {v}" for k, v in sorted(self.limits.items())) or "-",
            "설정 상한": ", ".join(f"{k} {v}" for k, v in sorted(self.configured_limits.items())) or "-",
        }


def _configured_limits(config: Config) -> dict[str, int]:
    return {
        ActionType.LIKE.value: int(config.get("actions.daily_limits.likes", 0)),
        ActionType.COMMENT.value: int(config.get("actions.daily_limits.comments", 0)),
    }


def current_state(conn: sqlite3.Connection) -> tuple[ActivityState, str, Optional[str]]:
    raw = get_state(conn, STATE_KEY, ActivityState.NORMAL.value) or ActivityState.NORMAL.value
    try:
        state = ActivityState(raw)
    except ValueError:
        state = ActivityState.NORMAL
    return state, get_state(conn, STATE_REASON_KEY, "") or "", get_state(conn, STATE_SINCE_KEY, None)


def status(
    conn: sqlite3.Connection, config: Config, *, now: Optional[datetime] = None
) -> ActivityStatus:
    """현재 상태와 적용 상한을 돌려준다. 상한은 설정값 이하로만 나온다."""
    now = now or datetime.now(timezone.utc)
    state, reason, since = current_state(conn)
    state, reason, since = _auto_recover(conn, config, state, reason, since, now)

    configured = _configured_limits(config)
    if state is ActivityState.STOPPED:
        limits = {key: 0 for key in configured}
    elif state is ActivityState.CAUTION:
        ratio = float(config.get("activity.caution_ratio", 0.5))
        ratio = min(max(ratio, 0.0), 1.0)  # 1을 넘겨 상한을 늘리지 못한다
        limits = {key: int(value * ratio) for key, value in configured.items()}
    else:
        limits = dict(configured)

    return ActivityStatus(
        state=state, reason=reason, since=since, limits=limits, configured_limits=configured
    )


def _auto_recover(
    conn: sqlite3.Connection,
    config: Config,
    state: ActivityState,
    reason: str,
    since: Optional[str],
    now: datetime,
) -> tuple[ActivityState, str, Optional[str]]:
    """CAUTION은 조용한 기간이 지나면 NORMAL로 되돌린다.

    STOPPED는 **자동으로 풀지 않는다** — 사람이 확인해야 한다.
    """
    if state is not ActivityState.CAUTION or not since:
        return state, reason, since
    hours = int(config.get("activity.caution_recovery_hours", 24))
    try:
        changed = datetime.fromisoformat(str(since))
    except ValueError:
        return state, reason, since
    if changed.tzinfo is None:
        changed = changed.replace(tzinfo=timezone.utc)
    if now - changed < timedelta(hours=max(hours, 1)):
        return state, reason, since

    _write_state(conn, ActivityState.NORMAL, "caution_recovered", now)
    logger.info("Activity 상태를 NORMAL로 되돌립니다(조용한 기간 %d시간 경과).", hours)
    return ActivityState.NORMAL, "caution_recovered", utc_now()


def observe(
    conn: sqlite3.Connection,
    signal: Optional[str],
    *,
    now: Optional[datetime] = None,
) -> ActivityState:
    """플랫폼 신호를 반영한다. **악화 방향으로만** 상태를 바꾼다."""
    now = now or datetime.now(timezone.utc)
    state, _reason, _since = current_state(conn)
    if not signal:
        return state

    token = str(signal).upper()
    if token in STOP_SIGNALS:
        target = ActivityState.STOPPED
    elif token in CAUTION_SIGNALS:
        target = ActivityState.CAUTION
    else:
        return state

    if target.severity <= state.severity:
        return state  # 좋아지는 방향은 observe가 하지 않는다(자동 증가 금지)

    _write_state(conn, target, token, now)
    logger.warning("Activity 상태 %s → %s (%s)", state.value, target.value, token)
    return target


def reset(conn: sqlite3.Connection, *, reason: str = "operator_reset") -> ActivityState:
    """운영자가 직접 상태를 되돌린다(STOPPED 해제는 사람만 할 수 있다)."""
    _write_state(conn, ActivityState.NORMAL, reason, datetime.now(timezone.utc))
    logger.info("Activity 상태를 운영자 요청으로 NORMAL로 되돌렸습니다.")
    return ActivityState.NORMAL


def _write_state(
    conn: sqlite3.Connection, state: ActivityState, reason: str, now: datetime
) -> None:
    set_state(conn, STATE_KEY, state.value)
    set_state(conn, STATE_REASON_KEY, reason)
    set_state(conn, STATE_SINCE_KEY, now.isoformat(timespec="seconds"))
    conn.commit()


def effective_config(config: Config, activity: ActivityStatus) -> Config:
    """활동 상태를 반영한 Config 사본(일일 상한만 낮춘다).

    RateLimiter가 이 Config를 읽으면 기존 한도 검사를 그대로 쓰면서
    CAUTION/STOPPED가 반영된다 — 별도 한도 시스템을 만들지 않는다.
    """
    from ..core.config import Config as ConfigType

    actions = {**config.raw.get("actions", {})}
    limits = {**actions.get("daily_limits", {})}
    limits["likes"] = activity.limit_for(ActionType.LIKE)
    limits["comments"] = activity.limit_for(ActionType.COMMENT)
    actions["daily_limits"] = limits
    raw = {**config.raw, "actions": actions}
    return ConfigType(raw=raw, path=config.path, base_dir=config.base_dir)
