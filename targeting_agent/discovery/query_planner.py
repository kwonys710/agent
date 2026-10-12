"""Query Planner — 성과 기반 검색어 회전과 쿨다운 (Phase 19B).

Scheduler는 "언제 돌릴지"만 정하고, "무엇을 검색할지"는 여기서 정한다.

규칙:
- 매 Run 전부 검색하지 않는다. 상한(기본 6개) 안에서 고른다.
- 약 70%는 성과 좋은 ACTIVE, 약 30%는 EXPERIMENTAL/저사용 검색어 → Discovery 다양성.
- 직전 Run과 **같은 조합**을 반복하지 않는다(은행이 부족하면 fallback).
- 성과가 반복적으로 나쁜 검색어는 COOLDOWN으로 쉬게 한다. 영구 삭제는 없다.

Utility는 결정론적이다(같은 데이터 → 같은 점수). LLM을 쓰지 않는다.
이 회전은 Discovery 품질 다양화 목적이며 탐지 회피와 무관하다.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional, Sequence

from ..core.config import Config
from ..core.database import get_state, set_state, utc_now
from ..core.logger import get_logger
from .query_bank import (
    QueryRecord,
    QueryStatus,
    list_queries,
    set_status,
    set_utility,
)

logger = get_logger("discovery.query_planner")

LAST_BATCH_KEY = "query_planner_last_batch"

# Utility 기본 가중치 — config(`query_planner.weights.*`)로 덮어쓸 수 있다.
# Dashboard에 중복 정의하지 않는다(여기가 single source).
DEFAULT_WEIGHTS: dict[str, float] = {
    "new_rate": 1.0,
    "unique_creator_rate": 0.8,
    "score_75_rate": 0.9,
    "score_82_rate": 0.6,
    "avg_score": 0.5,       # 0~1로 정규화한 평균 점수
    "duplicate_rate": -1.0,  # 유일한 negative
}
# 표본이 없는 검색어의 기본 utility. 한 번도 안 써 본 검색어를 바로 버리지 않기 위해
# 중간값을 준다(탐색 기회 보장).
UNSAMPLED_UTILITY = 0.5


@dataclass
class QueryPlan:
    """이번 Run에 쓸 검색어 묶음."""

    queries: list[str] = field(default_factory=list)
    query_ids: dict[str, int] = field(default_factory=dict)
    performers: list[str] = field(default_factory=list)
    explorers: list[str] = field(default_factory=list)
    repeated_batch: bool = False
    reason: str = ""

    def __bool__(self) -> bool:
        return bool(self.queries)


def weights_from_config(config: Config) -> dict[str, float]:
    override = config.section("query_planner.weights") or {}
    return {**DEFAULT_WEIGHTS, **{k: float(v) for k, v in override.items()}}


def compute_utility(
    record: QueryRecord, weights: Optional[dict[str, float]] = None
) -> Optional[float]:
    """검색어 성과를 하나의 점수로 모은다. 표본이 없으면 None."""
    values = weights or DEFAULT_WEIGHTS
    parts: dict[str, Optional[float]] = {
        "new_rate": record.new_rate,
        "unique_creator_rate": record.unique_creator_rate,
        "score_75_rate": record.score_75_rate,
        "score_82_rate": record.score_82_rate,
        "avg_score": (record.avg_target_score / 100.0) if record.avg_target_score else None,
        "duplicate_rate": record.duplicate_rate,
    }
    if all(value is None for value in parts.values()):
        return None

    total = 0.0
    weight_sum = 0.0
    for key, value in parts.items():
        if value is None:
            continue
        weight = float(values.get(key, 0.0))
        total += weight * min(max(float(value), 0.0), 1.0)
        weight_sum += abs(weight)
    if not weight_sum:
        return None
    # 0~1로 정규화해 읽기 쉽게 만든다(음수 가중치 때문에 하한은 0으로 자른다).
    return max(0.0, min(1.0, (total / weight_sum) + 0.5))


def refresh_utilities(conn: sqlite3.Connection, config: Config) -> int:
    """모든 검색어의 utility를 다시 계산해 저장한다(결정론적)."""
    weights = weights_from_config(config)
    updated = 0
    for record in list_queries(conn):
        utility = compute_utility(record, weights)
        if utility != record.utility:
            set_utility(conn, record.query_id, utility)
            updated += 1
    conn.commit()
    return updated


# --- Cooldown --------------------------------------------------------------
def apply_cooldowns(
    conn: sqlite3.Connection, config: Config, *, now: Optional[datetime] = None
) -> dict[str, list[str]]:
    """성과가 나쁜 검색어를 COOLDOWN으로, 기간이 끝난 것은 되돌린다.

    `{"cooldown": [...], "restored": [...], "promoted": [...]}`를 반환한다.
    """
    now = now or datetime.now(timezone.utc)
    min_runs = int(config.get("query_planner.cooldown.min_runs", 3))
    max_duplicate = float(config.get("query_planner.cooldown.duplicate_rate", 0.9))
    min_new = float(config.get("query_planner.cooldown.new_rate", 0.05))
    hours = int(config.get("query_planner.cooldown.hours", 72))
    promote_runs = int(config.get("query_planner.promote.min_runs", 2))
    promote_new = float(config.get("query_planner.promote.new_rate", 0.3))

    moved: dict[str, list[str]] = {"cooldown": [], "restored": [], "promoted": []}
    for record in list_queries(conn):
        if record.status is QueryStatus.DISABLED:
            continue

        if record.status is QueryStatus.COOLDOWN:
            if _cooldown_expired(record.cooldown_until, now):
                set_status(conn, record.query_id, QueryStatus.EXPERIMENTAL, reason="cooldown_expired")
                moved["restored"].append(record.query)
            continue

        if record.run_count >= min_runs:
            duplicate_rate = record.duplicate_rate
            new_rate = record.new_rate
            poor_duplicates = duplicate_rate is not None and duplicate_rate >= max_duplicate
            poor_new = new_rate is not None and new_rate <= min_new
            if poor_duplicates or poor_new:
                until = (now + timedelta(hours=hours)).isoformat(timespec="seconds")
                reason = "high_duplicate" if poor_duplicates else "low_new_rate"
                set_status(
                    conn, record.query_id, QueryStatus.COOLDOWN, reason=reason, cooldown_until=until
                )
                moved["cooldown"].append(record.query)
                continue

        if (
            record.status is QueryStatus.EXPERIMENTAL
            and record.run_count >= promote_runs
            and (record.new_rate or 0) >= promote_new
        ):
            set_status(conn, record.query_id, QueryStatus.ACTIVE, reason="good_new_rate")
            moved["promoted"].append(record.query)

    conn.commit()
    if any(moved.values()):
        logger.info(
            "Query 상태 변경: cooldown %d / 복귀 %d / 승격 %d",
            len(moved["cooldown"]),
            len(moved["restored"]),
            len(moved["promoted"]),
        )
    return moved


def _cooldown_expired(cooldown_until: Optional[str], now: datetime) -> bool:
    if not cooldown_until:
        return True
    try:
        until = datetime.fromisoformat(str(cooldown_until))
    except ValueError:
        return True
    if until.tzinfo is None:
        until = until.replace(tzinfo=timezone.utc)
    return now >= until


# --- 선택 ------------------------------------------------------------------
def plan_queries(
    conn: sqlite3.Connection,
    config: Config,
    *,
    limit: Optional[int] = None,
    now: Optional[datetime] = None,
) -> QueryPlan:
    """이번 Run의 검색어를 고른다(성과 70% + 탐색 30%, 직전 조합 반복 회피)."""
    now = now or datetime.now(timezone.utc)
    max_queries = int(limit or config.get("query_planner.max_queries_per_run", 6))
    explore_ratio = float(config.get("query_planner.explore_ratio", 0.3))

    refresh_utilities(conn, config)
    apply_cooldowns(conn, config, now=now)

    candidates = [
        record
        for record in list_queries(conn)
        if record.status.selectable
    ]
    if not candidates:
        return QueryPlan(reason="은행에 선택 가능한 검색어가 없습니다.")

    explore_target = max(1, round(max_queries * explore_ratio)) if max_queries > 1 else 0
    perform_target = max(0, max_queries - explore_target)

    performers = sorted(
        (r for r in candidates if r.status is QueryStatus.ACTIVE),
        key=lambda r: (-(r.utility if r.utility is not None else UNSAMPLED_UTILITY), r.query_id),
    )
    # 탐색군: 실행 횟수가 적은 것 우선(EXPERIMENTAL 먼저).
    explorers = sorted(
        candidates,
        key=lambda r: (
            0 if r.status is QueryStatus.EXPERIMENTAL else 1,
            r.run_count,
            _never_run_first(r.last_run_at),
            r.query_id,
        ),
    )

    previous = _last_batch(conn)
    plan = _select(performers, explorers, perform_target, explore_target, max_queries, avoid=previous)
    if not plan.queries:
        return QueryPlan(reason="선택 가능한 검색어가 없습니다.")

    if set(plan.queries) == previous and len(candidates) > max_queries:
        # 조합이 그대로면 성과 상위에서 한 칸 밀어 다른 묶음을 만든다.
        rotated = _select(
            performers[1:] + performers[:1],
            explorers[1:] + explorers[:1],
            perform_target,
            explore_target,
            max_queries,
            avoid=previous,
        )
        if rotated.queries and set(rotated.queries) != previous:
            plan = rotated
        else:
            plan.repeated_batch = True
            plan.reason = "은행이 부족해 직전 조합을 재사용합니다."
    elif set(plan.queries) == previous:
        plan.repeated_batch = True
        plan.reason = "은행이 부족해 직전 조합을 재사용합니다."

    _save_batch(conn, plan.queries)
    logger.info(
        "Query Plan: %d개 (성과 %s / 탐색 %s)%s",
        len(plan.queries),
        ",".join(plan.performers) or "-",
        ",".join(plan.explorers) or "-",
        " [조합 반복]" if plan.repeated_batch else "",
    )
    return plan


def _never_run_first(last_run_at: Optional[str]) -> str:
    """한 번도 안 돌린 검색어를 가장 앞으로(빈 문자열이 가장 작다)."""
    return last_run_at or ""


def _select(
    performers: Sequence[QueryRecord],
    explorers: Sequence[QueryRecord],
    perform_target: int,
    explore_target: int,
    max_queries: int,
    *,
    avoid: Optional[set[str]] = None,
) -> QueryPlan:
    plan = QueryPlan()
    chosen: dict[str, int] = {}

    def take(records: Sequence[QueryRecord], count: int, bucket: list[str]) -> None:
        for record in records:
            if len(chosen) >= max_queries or len(bucket) >= count:
                break
            if record.query in chosen:
                continue
            chosen[record.query] = record.query_id
            bucket.append(record.query)

    take(performers, perform_target, plan.performers)
    take(explorers, explore_target, plan.explorers)
    # 한쪽이 부족하면 나머지로 채운다(상한은 지킨다).
    if len(chosen) < max_queries:
        take(explorers, max_queries, plan.explorers)
    if len(chosen) < max_queries:
        take(performers, max_queries, plan.performers)

    plan.queries = list(chosen)
    plan.query_ids = dict(chosen)
    return plan


def _last_batch(conn: sqlite3.Connection) -> set[str]:
    raw = get_state(conn, LAST_BATCH_KEY, "") or ""
    try:
        return set(json.loads(raw)) if raw else set()
    except json.JSONDecodeError:
        return set()


def _save_batch(conn: sqlite3.Connection, queries: Sequence[str]) -> None:
    set_state(conn, LAST_BATCH_KEY, json.dumps(list(queries), ensure_ascii=False))
    conn.commit()


def planner_summary(conn: sqlite3.Connection) -> dict[str, object]:
    """Dashboard/리포트용 요약(읽기 전용)."""
    from .query_bank import counts_by_status

    records = list_queries(conn)
    sampled = [r for r in records if r.utility is not None]
    return {
        "bank_size": len(records),
        "status": counts_by_status(conn),
        "last_batch": sorted(_last_batch(conn)),
        "avg_utility": (sum(r.utility or 0 for r in sampled) / len(sampled)) if sampled else None,
        "never_run": sum(1 for r in records if not r.run_count),
    }
