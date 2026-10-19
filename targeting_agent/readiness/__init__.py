"""Live Readiness & Calibration (Phase 18D).

실제 운영 데이터를 **읽어서만** 판단한다 — LLM을 호출하지 않고, 설정을 바꾸지 않고,
Instagram에 쓰지 않는다. 같은 DB면 항상 같은 결과가 나온다(결정론적).
"""
from __future__ import annotations

from .snapshot import BaselineSnapshot, collect_baseline, pct

__all__ = ["BaselineSnapshot", "collect_baseline", "pct"]
