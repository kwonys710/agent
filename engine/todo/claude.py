"""Claude interpretation boundary.

이번 단계에서는 passthrough(no-op) 만 구현한다.
- anthropic import 없음, 실제 API 호출 없음.
- client is None (기본) → 입력 candidate 를 그대로 돌려준다.
- 향후 client 주입 시 요약/그룹핑/우선순위 표현을 담당하게 될 경계.
"""
from __future__ import annotations

import dataclasses


@dataclasses.dataclass(frozen=True)
class InterpretationResult:
    candidates: tuple
    claude_used: bool
    claude_task_count: int


def interpret_candidates(candidates, *, client=None) -> InterpretationResult:
    if client is None:
        return InterpretationResult(
            candidates=tuple(candidates),
            claude_used=False,
            claude_task_count=0,
        )
    # 향후: client 를 사용한 해석. 이번 커밋 범위 밖.
    raise NotImplementedError(
        "Claude interpretation 은 아직 구현되지 않았습니다 (이번 커밋은 passthrough only)."
    )
