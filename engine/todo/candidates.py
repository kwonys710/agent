"""Rule 결과 → TodoCandidate union / dedupe / primary category.

- 입력은 상위에서 주입되는 list[TaskState] + optional changes(Mapping[internal_task_id, TodoChange]).
- DB 접근 없음. datetime.now() 내부 호출 없음 (today 주입).
- dedupe key 는 internal_task_id (source_task_id / row_key 기준 dedupe 금지).
"""
from __future__ import annotations

import dataclasses
import datetime as dt
from typing import Mapping, Optional

from engine.wbs.normalize import NormalizedStatus
from .rules import (
    CATEGORY_FOLLOW_UP,
    CATEGORY_NEEDS_CONFIRMATION,
    RuleHit,
    category_priority_index,
    confirmation_hit,
    evaluate_task,
    follow_up_reason,
    has_confirmation_issue,
    is_done,
)


@dataclasses.dataclass(frozen=True)
class TodoChange:
    """Commit 3 sync report + old TaskState 에서 Commit 5 orchestration 이 구성할 변경 정보.

    Commit 4 에서는 테스트가 직접 생성한다. Commit 3 코드는 수정하지 않는다.
    old/new progress 가 없으면 progress 는 그대로 significant 로 간주한다(threshold 미적용).
    """

    changed_fields: tuple = ()
    row_hash_changed: bool = False
    old_progress: Optional[float] = None
    new_progress: Optional[float] = None


@dataclasses.dataclass(frozen=True)
class SourceRef:
    project_id: str
    spreadsheet_id: str
    sheet_name: str
    internal_task_id: str
    source_task_id: Optional[str]
    row_key: str
    row_hash: str


@dataclasses.dataclass(frozen=True)
class TodoCandidate:
    internal_task_id: str
    source_task_id: Optional[str]
    row_key: str

    task_name: Optional[str]
    owner: Optional[str]
    start_date: Optional[str]
    due_date: Optional[str]
    progress: Optional[float]
    raw_status: Optional[str]
    normalized_status: Optional[str]
    priority: Optional[str]
    dependency: tuple
    notes: Optional[str]

    category: str
    also_categories: tuple

    reason: str
    needs_confirmation: bool

    changed_fields: tuple
    row_hash_changed: bool

    source_ref: SourceRef


@dataclasses.dataclass(frozen=True)
class CandidateBuildResult:
    candidates: tuple
    excluded_done: tuple
    excluded_not_due: tuple
    excluded_inactive: tuple
    needs_confirmation_count: int
    category_counts: dict


def _due_sort_key(candidate: TodoCandidate):
    due = candidate.due_date
    return (
        category_priority_index(candidate.category),
        due is None,
        due or "",
        candidate.task_name or "",
        candidate.internal_task_id,
    )


def sort_candidates(candidates) -> list:
    """deterministic 정렬: primary priority → due_date(asc, None last) → task_name → internal_task_id."""
    return sorted(candidates, key=_due_sort_key)


def _source_ref(task) -> SourceRef:
    return SourceRef(
        project_id=task.project_id,
        spreadsheet_id=task.spreadsheet_id,
        sheet_name=task.sheet_name,
        internal_task_id=task.internal_task_id,
        source_task_id=task.source_task_id,
        row_key=task.row_key,
        row_hash=task.row_hash,
    )


def _build_candidate(task, hits, change) -> TodoCandidate:
    ordered = sorted(hits, key=lambda hit: category_priority_index(hit.category))
    primary = ordered[0]
    also = tuple(hit.category for hit in ordered[1:])

    reason = " ".join(hit.reason for hit in ordered).strip()

    needs_confirmation = (
        any(hit.category == CATEGORY_NEEDS_CONFIRMATION for hit in ordered)
        or bool(task.parse_errors)
        or task.normalized_status == NormalizedStatus.UNKNOWN.value
    )

    changed_fields = tuple(change.changed_fields) if change is not None else ()
    row_hash_changed = bool(change.row_hash_changed) if change is not None else False

    return TodoCandidate(
        internal_task_id=task.internal_task_id,
        source_task_id=task.source_task_id,
        row_key=task.row_key,
        task_name=task.task_name,
        owner=task.owner,
        start_date=task.start_date,
        due_date=task.end_date,
        progress=task.progress,
        raw_status=task.raw_status,
        normalized_status=task.normalized_status,
        priority=task.priority,
        dependency=tuple(task.dependency or ()),
        notes=task.notes,
        category=primary.category,
        also_categories=also,
        reason=reason,
        needs_confirmation=needs_confirmation,
        changed_fields=changed_fields,
        row_hash_changed=row_hash_changed,
        source_ref=_source_ref(task),
    )


def build_candidates(
    tasks,
    *,
    today: dt.date,
    config,
    changes: Optional[Mapping] = None,
) -> CandidateBuildResult:
    changes = changes or {}

    candidates: list = []
    excluded_done: list = []
    excluded_not_due: list = []
    excluded_inactive: list = []

    for task in tasks:
        if not task.is_active:
            excluded_inactive.append(task.internal_task_id)
            continue

        done = is_done(task, config)
        confirmation_issue = has_confirmation_issue(task)

        if done:
            # 완료로 추정되지만 원본 데이터 확인이 필요하면 조용히 버리지 않고
            # NEEDS_CONFIRMATION candidate 만 생성한다 (일반 업무 category 는 생성 안 함).
            hit = confirmation_hit(task, done=True) if confirmation_issue else None
            if hit is None:
                excluded_done.append(task.internal_task_id)
            else:
                candidates.append(_build_candidate(task, [hit], None))
            continue

        change = changes.get(task.internal_task_id)
        hits = evaluate_task(task, today=today, config=config, change=change)
        if not hits:
            excluded_not_due.append(task.internal_task_id)
            continue

        candidates.append(_build_candidate(task, hits, change))

    ordered = tuple(sort_candidates(candidates))

    category_counts: dict = {}
    for candidate in ordered:
        category_counts[candidate.category] = category_counts.get(candidate.category, 0) + 1

    needs_confirmation_count = sum(1 for c in ordered if c.needs_confirmation)

    return CandidateBuildResult(
        candidates=ordered,
        excluded_done=tuple(excluded_done),
        excluded_not_due=tuple(excluded_not_due),
        excluded_inactive=tuple(excluded_inactive),
        needs_confirmation_count=needs_confirmation_count,
        category_counts=category_counts,
    )


def _merge_categories(existing_primary: str, existing_also: tuple, extra: str) -> tuple:
    seen = [existing_primary, *existing_also]
    if extra not in seen:
        seen.append(extra)
    ordered = sorted(dict.fromkeys(seen), key=category_priority_index)
    return ordered[0], tuple(ordered[1:])


def merge_follow_up(candidates, previous_unresolved, states_by_id, config, *, previous_date=None):
    """FOLLOW_UP = 이전 Daily To-Do 실행일에 있었으나 아직 해결되지 않은 업무.

    - previous_unresolved: 오늘보다 이전인 가장 최근 실행일의 daily_todo_item 중 resolved == 0
      (기준일 선택은 persist.get_previous_unresolved_snapshot 책임 — 여기서는 internal_task_id 만 사용).
    - 현재 WBS state 와 다시 대조: 현재 active + not done + state 존재 인 것만 FOLLOW_UP 후보.
    - 오늘 이미 다른 category 후보인 task 는 FOLLOW_UP 을 also/reason 에 merge (중복 candidate 금지).
    - 오늘 다른 category 가 전혀 없는 task 는 FOLLOW_UP 단독 candidate 로 추가.

    returns (merged_candidates: tuple, follow_up_ids: frozenset)
    """
    reason_text = follow_up_reason(previous_date)

    follow_up_ids = set()
    for item in previous_unresolved or []:
        internal_task_id = item["internal_task_id"] if isinstance(item, dict) else item
        state = states_by_id.get(internal_task_id)
        if state is None or not state.is_active:
            continue
        if is_done(state, config):
            continue
        follow_up_ids.add(internal_task_id)

    existing_ids = {c.internal_task_id for c in candidates}
    merged: list = []

    for candidate in candidates:
        if candidate.internal_task_id in follow_up_ids:
            primary, also = _merge_categories(candidate.category, candidate.also_categories, CATEGORY_FOLLOW_UP)
            reason = (candidate.reason + " " + reason_text).strip()
            merged.append(dataclasses.replace(
                candidate, category=primary, also_categories=also, reason=reason,
            ))
        else:
            merged.append(candidate)

    for internal_task_id in follow_up_ids - existing_ids:
        state = states_by_id[internal_task_id]
        hit = RuleHit(CATEGORY_FOLLOW_UP, reason_text)
        merged.append(_build_candidate(state, [hit], None))

    return tuple(sort_candidates(merged)), frozenset(follow_up_ids)
