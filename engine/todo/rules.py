"""Daily To-Do deterministic rule engine (pure functions).

- 개별 TaskState 에 대해 category 판정 + 사실 기반 reason 생성.
- `today` 는 반드시 외부에서 주입한다 (datetime.now() 내부 호출 금지).
- 상태 문자열은 정규화 vocabulary(DONE/BLOCKED/UNKNOWN ...)만 참조한다.
  WBS 원본 헤더/상태값은 코드에 하드코딩하지 않는다 (config.status_map / blocked_keywords).
- Claude/우선순위 추측 없음 — 근거 없는 판단("중요", "긴급") 금지.

이 커밋 범위 밖: FOLLOW_UP, daily_todo persist, JSON renderer, CLI, Claude, Slack.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
from typing import Optional

from engine.wbs.normalize import NormalizedStatus, parse_date

CATEGORY_OVERDUE = "OVERDUE"
CATEGORY_BLOCKED = "BLOCKED"
CATEGORY_DUE_TODAY = "DUE_TODAY"
CATEGORY_CHANGED = "CHANGED"
CATEGORY_DUE_SOON = "DUE_SOON"
CATEGORY_START_TODAY = "START_TODAY"
CATEGORY_NEEDS_CONFIRMATION = "NEEDS_CONFIRMATION"

# primary category 결정용 우선순위. FOLLOW_UP 은 Commit 5 에서 추가 예정.
CATEGORY_PRIORITY = (
    CATEGORY_OVERDUE,
    CATEGORY_BLOCKED,
    CATEGORY_DUE_TODAY,
    CATEGORY_CHANGED,
    CATEGORY_DUE_SOON,
    CATEGORY_START_TODAY,
    CATEGORY_NEEDS_CONFIRMATION,
)

_PRIORITY_INDEX = {name: idx for idx, name in enumerate(CATEGORY_PRIORITY)}

# CHANGED reason 용 필드 표시명 (UI label — WBS 원본 헤더와 무관).
FIELD_LABELS = {
    "task_name": "작업명",
    "owner": "담당자",
    "start_date": "시작일",
    "end_date": "종료일",
    "progress": "진척률",
    "normalized_status": "상태",
    "priority": "우선순위",
    "dependency": "선행작업",
    "notes": "비고",
    "phase": "단계",
    "project": "프로젝트",
    "raw_status": "원본 상태값",
}


def category_priority_index(category: str) -> int:
    return _PRIORITY_INDEX.get(category, len(CATEGORY_PRIORITY))


@dataclasses.dataclass(frozen=True)
class RuleHit:
    category: str
    reason: str
    detail: dict = dataclasses.field(default_factory=dict)


def _to_date(value: object) -> Optional[dt.date]:
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    parsed, ok = parse_date(value)
    return parsed if ok else None


def is_done(task, config) -> bool:
    """완료 판정: normalized_status == DONE 또는 progress >= progress_done_threshold.

    deterministic 완료 판정 helper. 이 함수의 의미는 confirmation override 와 무관하게 고정이다.
    """
    if task.normalized_status == NormalizedStatus.DONE.value:
        return True
    if task.progress is not None and task.progress >= config.progress_done_threshold:
        return True
    return False


def has_confirmation_issue(task) -> bool:
    """원본 데이터 해석 불가 신호: normalized_status == UNKNOWN 또는 parse_errors 존재."""
    if task.normalized_status == NormalizedStatus.UNKNOWN.value:
        return True
    return bool(task.parse_errors)


def _blocked_keyword(notes: Optional[str], keywords) -> Optional[str]:
    if not notes or not keywords:
        return None
    folded = notes.casefold()
    for keyword in keywords:
        candidate = str(keyword).strip()
        if candidate and candidate.casefold() in folded:
            return candidate
    return None


def _significant_changed_fields(change, config) -> list:
    declared = list(config.change_significant_fields or [])
    changed = set(change.changed_fields or ())
    significant = [field for field in declared if field in changed]

    if "progress" in significant:
        old_p = change.old_progress
        new_p = change.new_progress
        if old_p is not None and new_p is not None:
            if abs(new_p - old_p) < config.progress_change_min_delta:
                significant = [f for f in significant if f != "progress"]
    return significant


def _changed_reason(significant_fields: list, config) -> str:
    ordered = [f for f in (config.change_significant_fields or []) if f in set(significant_fields)]
    labels = [FIELD_LABELS.get(field, field) for field in ordered]
    return ", ".join(labels) + "이(가) 이전 WBS 상태에서 변경됨."


def evaluate_task(task, *, today: dt.date, config, change=None) -> list:
    """TaskState -> list[RuleHit]. 완료/비활성 task 는 상위(build_candidates)에서 제외된다."""
    hits: list = []

    if is_done(task, config):
        # 완료 업무는 모든 category 에서 제외 (완료 알림은 향후 report/feed 책임).
        return hits

    end_date = _to_date(task.end_date)
    start_date = _to_date(task.start_date)

    if end_date is not None:
        if end_date < today:
            days = (today - end_date).days
            hits.append(RuleHit(
                CATEGORY_OVERDUE,
                f"종료일 {end_date.isoformat()}이(가) 오늘({today.isoformat()})보다 {days}일 지남.",
                {"days_overdue": days},
            ))
        elif end_date == today:
            hits.append(RuleHit(
                CATEGORY_DUE_TODAY,
                f"종료일이 오늘({today.isoformat()})임.",
            ))
        else:
            horizon = today + dt.timedelta(days=config.due_soon_days)
            if today < end_date <= horizon:
                remaining = (end_date - today).days
                hits.append(RuleHit(
                    CATEGORY_DUE_SOON,
                    f"종료일({end_date.isoformat()})이 {remaining}일 이내로 임박함.",
                    {"days_remaining": remaining},
                ))

    if start_date is not None and start_date == today:
        hits.append(RuleHit(
            CATEGORY_START_TODAY,
            f"시작일이 오늘({today.isoformat()})임.",
        ))

    blocked_hit = _blocked_hit(task, config)
    if blocked_hit is not None:
        hits.append(blocked_hit)

    if change is not None:
        significant = _significant_changed_fields(change, config)
        if significant:
            hits.append(RuleHit(
                CATEGORY_CHANGED,
                _changed_reason(significant, config),
                {"significant_fields": tuple(significant)},
            ))

    hit = confirmation_hit(task)
    if hit is not None:
        hits.append(hit)

    hits.sort(key=lambda hit: category_priority_index(hit.category))
    return hits


def _blocked_hit(task, config) -> Optional[RuleHit]:
    if task.normalized_status == NormalizedStatus.BLOCKED.value:
        return RuleHit(CATEGORY_BLOCKED, "정규화 상태가 BLOCKED임.", {"source": "status"})
    keyword = _blocked_keyword(task.notes, config.blocked_keywords)
    if keyword is not None:
        return RuleHit(
            CATEGORY_BLOCKED,
            f"비고에서 차단 키워드 '{keyword}'가 확인됨.",
            {"source": "notes", "keyword": keyword},
        )
    return None


def confirmation_hit(task, *, done: bool = False) -> Optional[RuleHit]:
    """UNKNOWN 상태 / parse_errors 기반 NEEDS_CONFIRMATION hit.

    done=True 이면 "완료로 보이지만 원본 데이터 확인이 필요" 라는 사실만 덧붙인다
    (근거 없는 판단 없음).
    """
    parts: list = []
    if task.normalized_status == NormalizedStatus.UNKNOWN.value:
        raw = task.raw_status if task.raw_status is not None else ""
        parts.append(f"정규화할 수 없는 상태값('{raw}')")
    if task.parse_errors:
        parts.append("정규화 경고: " + ", ".join(task.parse_errors))
    if not parts:
        return None

    message = " / ".join(parts)
    if done:
        message += " (완료로 보이나 원본 데이터 확인이 필요함)"
    return RuleHit(
        CATEGORY_NEEDS_CONFIRMATION,
        message + " — 확인 필요.",
        {"parse_errors": tuple(task.parse_errors or ()), "done_estimated": done},
    )
