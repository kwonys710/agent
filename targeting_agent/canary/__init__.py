"""Limited Live Canary (Phase 18E).

실제 Instagram LIKE를 아주 적은 수만 수행해 LIVE 경로를 검증한다.
COMMENT / FOLLOW / DM / 저장 / 공유는 구현하지 않는다.
"""
from __future__ import annotations

from .runner import CanaryRunResult, format_result, live_override, run_canary
from .state import CanaryState, load_state, save_state, state_path

__all__ = [
    "CanaryRunResult",
    "CanaryState",
    "format_result",
    "live_override",
    "run_canary",
    "load_state",
    "save_state",
    "state_path",
]
