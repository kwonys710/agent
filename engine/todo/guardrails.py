"""Deterministic candidate guardrail.

이번 커밋에서는 후보 개수 기준(MAX_TODO_CANDIDATES)만 실제 동작으로 구현한다.
MAX_CHANGED_TASKS_PER_RUN / MAX_CLAUDE_TASKS_PER_RUN / MAX_TOKEN_BUDGET 는
Claude integration(Commit 5+)에서 추가한다 — 여기에 미리 넣지 않는다.

초과 시 run 전체를 실패시키지 않는다:
- priority 순으로 상위 N 개만 선택
- needs_review=True, guardrail_hit="MAX_TODO_CANDIDATES"
- total_candidates / omitted_count 로 "나머지가 있었다"는 사실을 보존
"""
from __future__ import annotations

import dataclasses

from .candidates import sort_candidates

MAX_TODO_CANDIDATES = 60


@dataclasses.dataclass(frozen=True)
class GuardrailResult:
    ok: bool
    needs_review: bool
    guardrail_hit: str | None
    total_candidates: int
    selected: tuple
    omitted_count: int


def apply_guardrails(candidates, *, max_todo_candidates: int = MAX_TODO_CANDIDATES) -> GuardrailResult:
    ordered = tuple(sort_candidates(candidates))
    total = len(ordered)

    if total <= max_todo_candidates:
        return GuardrailResult(
            ok=True,
            needs_review=False,
            guardrail_hit=None,
            total_candidates=total,
            selected=ordered,
            omitted_count=0,
        )

    selected = ordered[:max_todo_candidates]
    return GuardrailResult(
        ok=False,
        needs_review=True,
        guardrail_hit="MAX_TODO_CANDIDATES",
        total_candidates=total,
        selected=selected,
        omitted_count=total - len(selected),
    )
