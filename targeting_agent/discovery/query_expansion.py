"""Dynamic Query Expansion — 고득점 후보에서 새 검색어 후보 만들기 (Phase 19C).

입력은 **사람이 확인한 신호와 높은 Target Score**다.
Agent 자신의 LIKE/COMMENT나 Autopilot 자동 승인은 근거로 쓰지 않는다(자기 강화 방지).

비용 규칙:
- 후보마다 Claude를 부르지 않는다. 주기적 Batch(기본 하루 1회)만 수행한다.
- 조합 생성은 **결정론적**이다. Claude는 쓰지 않아도 동작한다(선택적 보강).
- 새 검색어는 바로 ACTIVE가 아니라 EXPERIMENTAL로 들어간다 — 실제 성과로 승격된다.
"""
from __future__ import annotations

import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional, Sequence

from ..core.config import Config
from ..core.database import get_state, set_state, utc_now
from ..core.logger import get_logger
from .query_bank import ORIGIN_EXPANSION, QueryStatus, add_query, get_query

logger = get_logger("discovery.query_expansion")

LAST_RUN_KEY = "query_expansion_last_run"

STATUS_OK = "OK"
STATUS_SKIPPED = "SKIPPED"
STATUS_NOT_ENOUGH_DATA = "NOT_ENOUGH_DATA"

# 조합에 쓰지 않는 일반어(이것만으로는 검색 가치가 낮다).
STOP_TERMS = frozenset(
    {
        "일상", "영상", "오늘", "하루", "진짜", "완전", "그냥", "너무", "정말",
        "reels", "reel", "instagram", "instagood", "fyp", "daily", "vlog",
    }
)


@dataclass
class ExpansionPlan:
    """확장 결과. DB에 반영된 것과 버린 것을 모두 남긴다."""

    status: str = STATUS_OK
    added: list[str] = field(default_factory=list)
    rejected: dict[str, str] = field(default_factory=dict)
    sources: int = 0
    terms: list[str] = field(default_factory=list)
    message: str = ""

    def reject(self, query: str, reason: str) -> None:
        self.rejected[query] = reason


def _normalize_term(term: Any) -> str:
    text = str(term or "").strip().lstrip("#").lower()
    return text if 1 < len(text) <= 20 else ""


def collect_terms(
    conn: sqlite3.Connection, *, min_score: float, limit: int = 50
) -> tuple[list[str], int]:
    """고득점 후보와 사람 Feedback(GOOD_TARGET)에서 term을 모은다.

    Agent의 Action 기록은 쓰지 않는다 — 사람의 판단과 점수만 본다.
    """
    rows = conn.execute(
        "SELECT m.media_pk, m.caption, m.hashtags, m.target_score "
        "FROM candidate_media m "
        "WHERE m.target_score IS NOT NULL AND m.target_score >= ? "
        "ORDER BY m.target_score DESC LIMIT ?",
        (float(min_score), int(limit)),
    ).fetchall()
    good = conn.execute(
        "SELECT m.media_pk, m.caption, m.hashtags, m.target_score "
        "FROM feedback f JOIN candidate_media m ON m.media_pk = f.media_pk "
        "WHERE f.feedback_type IN ('GOOD_TARGET', 'RESPONDED', 'FOLLOWED') LIMIT ?",
        (int(limit),),
    ).fetchall()

    seen: set[int] = set()
    counter: Counter[str] = Counter()
    for row in list(rows) + list(good):
        media_pk = int(row["media_pk"])
        if media_pk in seen:
            continue
        seen.add(media_pk)
        for term in _terms_of_row(conn, row):
            counter[term] += 1

    ordered = [term for term, _count in counter.most_common() if term not in STOP_TERMS]
    return ordered, len(seen)


def _terms_of_row(conn: sqlite3.Connection, row: sqlite3.Row) -> list[str]:
    """후보 1건의 해시태그 + Claude topics에서 term을 뽑는다."""
    import json

    terms: list[str] = []
    raw_hashtags = row["hashtags"]
    if raw_hashtags:
        try:
            for tag in json.loads(raw_hashtags) or []:
                normalized = _normalize_term(tag)
                if normalized:
                    terms.append(normalized)
        except (json.JSONDecodeError, TypeError):
            pass

    analysis = conn.execute(
        "SELECT payload FROM media_analysis WHERE media_pk = ? "
        "ORDER BY (analyzer = 'claude_code') DESC, analyzed_at DESC LIMIT 1",
        (int(row["media_pk"]),),
    ).fetchone()
    if analysis is not None:
        try:
            payload = json.loads(analysis["payload"]) or {}
        except (json.JSONDecodeError, TypeError):
            payload = {}
        for key in ("topics", "keywords"):
            for value in payload.get(key, []) or []:
                normalized = _normalize_term(value)
                if normalized:
                    terms.append(normalized)
    return terms


def combine(terms: Sequence[str], *, limit: int, pair_with: Sequence[str] = ()) -> list[str]:
    """term을 2개씩 묶어 검색어 후보를 만든다(결정론적: 입력 순서 그대로)."""
    anchors = [t for t in (pair_with or terms[:3]) if t]
    combos: list[str] = []
    for anchor in anchors:
        for term in terms:
            if term == anchor:
                continue
            candidate = f"{anchor} {term}"
            if candidate not in combos:
                combos.append(candidate)
            if len(combos) >= limit * 3:  # 뒤에서 기존/금지 검색어를 걸러내므로 넉넉히 만든다
                return combos
    return combos


def due(
    conn: sqlite3.Connection, config: Config, *, now: Optional[datetime] = None
) -> bool:
    """Batch 주기가 됐는지(기본 24시간)."""
    now = now or datetime.now(timezone.utc)
    hours = int(config.get("query_planner.expansion.interval_hours", 24))
    raw = get_state(conn, LAST_RUN_KEY, "")
    if not raw:
        return True
    try:
        last = datetime.fromisoformat(str(raw))
    except ValueError:
        return True
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return now - last >= timedelta(hours=max(hours, 1))


def expand_queries(
    conn: sqlite3.Connection,
    config: Config,
    *,
    now: Optional[datetime] = None,
    force: bool = False,
) -> ExpansionPlan:
    """신규 검색어 후보를 만들어 EXPERIMENTAL로 추가한다(주기적 Batch)."""
    plan = ExpansionPlan()
    if not bool(config.get("query_planner.expansion.enabled", True)):
        plan.status = STATUS_SKIPPED
        plan.message = "query_planner.expansion.enabled=false"
        return plan
    if not force and not due(conn, config, now=now):
        plan.status = STATUS_SKIPPED
        plan.message = "확장 주기가 아직 되지 않았습니다(하루 1회)."
        return plan

    min_new = int(config.get("query_planner.expansion.min_new_candidates", 10))
    max_new = int(config.get("query_planner.expansion.max_new_queries", 5))
    min_score = float(config.get("query_planner.expansion.min_score", 82))

    terms, sources = collect_terms(conn, min_score=min_score)
    plan.sources = sources
    plan.terms = terms[:10]
    if sources < min_new:
        plan.status = STATUS_NOT_ENOUGH_DATA
        plan.message = f"고득점 후보 {sources}건 — 최소 {min_new}건 필요"
        return plan
    if len(terms) < 2:
        plan.status = STATUS_NOT_ENOUGH_DATA
        plan.message = "조합할 term이 부족합니다."
        return plan

    for candidate in combine(terms, limit=max_new):
        if len(plan.added) >= max_new:
            break
        if get_query(conn, candidate) is not None:
            plan.reject(candidate, "already_in_bank")
            continue
        query_id = add_query(
            conn,
            candidate,
            category="expansion",
            origin=ORIGIN_EXPANSION,
            status=QueryStatus.EXPERIMENTAL,  # 바로 ACTIVE로 올리지 않는다
        )
        if query_id is None:
            plan.reject(candidate, "already_in_bank")
            continue
        plan.added.append(candidate)

    set_state(conn, LAST_RUN_KEY, (now or datetime.now(timezone.utc)).isoformat(timespec="seconds"))
    conn.commit()
    logger.info(
        "Query Expansion: 후보 %d건(출처 %d건) 추가, 제외 %d건",
        len(plan.added),
        plan.sources,
        len(plan.rejected),
    )
    return plan


def expansion_summary(conn: sqlite3.Connection) -> dict[str, Any]:
    """확장으로 들어온 검색어 현황(모니터링용)."""
    rows = conn.execute(
        "SELECT status, COUNT(*) AS n FROM query_bank WHERE origin = ? GROUP BY status",
        (ORIGIN_EXPANSION,),
    ).fetchall()
    return {
        "last_run_at": get_state(conn, LAST_RUN_KEY, None),
        "by_status": {str(row["status"]): int(row["n"]) for row in rows},
        "total": sum(int(row["n"]) for row in rows),
    }
