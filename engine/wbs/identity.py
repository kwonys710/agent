"""WBS task identity matching (deterministic, 비-fuzzy).

새로 normalize된 row 를 기존 wbs_task_state 와 매칭한다.
매칭 우선순위:

  MATCH 1  source_task_id 가 active state 중 정확히 1건 → 그 internal_task_id 유지
  MATCH 2  source_identity_key 가 active state 중 정확히 1건 → 그 internal_task_id 유지
  REACTIVATE  active 매칭이 없고, inactive state 중 source_task_id 또는
              source_identity_key 가 정확히 1건이며 active 충돌이 없음 → 같은 id 재활성
  CLAIMED COLLISION  매칭 가능한 기존 task 가 있으나 이번 sync 의 앞선 row 가 이미
              그 id 를 점유 → 새 id 를 받되 NEW 가 아니라 AMBIGUOUS 로 분류
  NEW  같은 키를 가진 기존 task 자체가 없어 안전하게 신규인 경우 → 새 internal_task_id

원칙:
- row_key(현재 위치) 는 identity 로 쓰지 않는다.
- source_identity_key uniqueness 를 가정하지 않는다 (0 / 1 / 2건 이상 분기).
- 애매하면(ambiguous) 기존 task 에 임의 연결하지 않는다 — 새 id + "ambiguous_identity_match".
- 문자열 유사도 / fuzzy matching 없음.
"""
from __future__ import annotations

import dataclasses
from typing import Optional

from .normalize import NormalizedTask
from .state import TaskState

KIND_SOURCE_TASK_ID = "source_task_id"
KIND_SOURCE_IDENTITY_KEY = "source_identity_key"
KIND_REACTIVATE = "reactivate"
KIND_NEW = "new"
KIND_AMBIGUOUS = "ambiguous"

REASON_CLAIMED = "existing_task_already_claimed"


@dataclasses.dataclass(frozen=True)
class MatchResult:
    internal_task_id: Optional[str]  # None => 새 task 발급 필요
    kind: str
    reactivated: bool = False
    ambiguous_reason: Optional[str] = None


def _by_source_task_id(states: list, source_task_id: Optional[str], claimed: set) -> list:
    if source_task_id is None:
        return []
    return [
        s
        for s in states
        if s.source_task_id == source_task_id and s.internal_task_id not in claimed
    ]


def _by_identity_key(states: list, identity_key: Optional[str], claimed: set) -> list:
    if not identity_key:
        return []
    return [
        s
        for s in states
        if s.source_identity_key == identity_key and s.internal_task_id not in claimed
    ]


def match_task(
    task: NormalizedTask,
    active_states: list,
    inactive_states: list,
    claimed: set,
) -> MatchResult:
    sid = task.source_task_id
    idk = task.source_identity_key

    active_sid = _by_source_task_id(active_states, sid, claimed)
    active_idk = _by_identity_key(active_states, idk, claimed)

    # --- MATCH 1: source_task_id 정확히 1건 ---
    if len(active_sid) == 1:
        chosen = active_sid[0]
        if len(active_idk) == 1 and active_idk[0].internal_task_id != chosen.internal_task_id:
            # source_task_id 후보와 source_identity_key 후보가 서로 다른 task 를 가리킴
            return MatchResult(None, KIND_AMBIGUOUS,
                               ambiguous_reason="source_task_id_and_identity_key_disagree")
        return MatchResult(chosen.internal_task_id, KIND_SOURCE_TASK_ID)

    # --- source_task_id 가 2건 이상: identity_key 로만 구제 시도 ---
    if len(active_sid) >= 2:
        if len(active_idk) == 1:
            return MatchResult(active_idk[0].internal_task_id, KIND_SOURCE_IDENTITY_KEY)
        return MatchResult(None, KIND_AMBIGUOUS, ambiguous_reason="duplicate_source_task_id")

    # --- MATCH 2: source_identity_key 정확히 1건 (source_task_id 매칭 0건) ---
    if len(active_idk) == 1:
        return MatchResult(active_idk[0].internal_task_id, KIND_SOURCE_IDENTITY_KEY)
    if len(active_idk) >= 2:
        return MatchResult(None, KIND_AMBIGUOUS, ambiguous_reason="multiple_identity_key_matches")

    # --- REACTIVATE: inactive state 에서 정확히 1건, active 충돌 없음 ---
    active_has_sid = sid is not None and any(s.source_task_id == sid for s in active_states)
    active_has_idk = bool(idk) and any(s.source_identity_key == idk for s in active_states)

    inactive_sid = _by_source_task_id(inactive_states, sid, claimed)
    if len(inactive_sid) == 1 and not active_has_sid:
        return MatchResult(inactive_sid[0].internal_task_id, KIND_REACTIVATE, reactivated=True)

    inactive_idk = _by_identity_key(inactive_states, idk, claimed)
    if len(inactive_idk) == 1 and not active_has_idk:
        return MatchResult(inactive_idk[0].internal_task_id, KIND_REACTIVATE, reactivated=True)

    # --- CLAIMED COLLISION ---
    # 여기까지 왔다는 것은 정상 규칙으로 안전한 1:1 매칭을 찾지 못했다는 뜻이다.
    # 그런데 이 row 와 같은 source_task_id / source_identity_key 를 가진 기존 task 가
    # 존재하고, 그 중 하나 이상이 이번 sync 의 앞선 row 에 의해 이미 점유(claimed)되어
    # 있다면 — 이 row 는 "기존 task 일 가능성은 있으나 안전한 identity 결정 불가" 상태다.
    # 앞선 row 의 매칭을 취소하지 않고, 이 row 는 새 id 를 받되 NEW 가 아니라 AMBIGUOUS 로 분류한다.
    collision = [
        s
        for s in (active_states + inactive_states)
        if (sid is not None and s.source_task_id == sid)
        or (idk and s.source_identity_key == idk)
    ]
    if any(s.internal_task_id in claimed for s in collision):
        return MatchResult(None, KIND_AMBIGUOUS, ambiguous_reason=REASON_CLAIMED)

    # --- NEW ---
    return MatchResult(None, KIND_NEW)
