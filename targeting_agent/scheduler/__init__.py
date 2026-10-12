"""Scheduled Run(Phase 15) — 로컬 배치 실행.

Windows Task Scheduler가 하루 한 번 BAT을 실행하면, 이 패키지가
inbox 처리 → 신규 후보 분석 → 통계 집계 → Daily Summary HTML 생성까지 하고 종료한다.

실제 Instagram Action은 실행하지 않는다(승인된 Action이 있어도 실행하지 않는다).
"""
from .lock import SchedulerLock
from .runner import ScheduledRunResult, run_scheduled

__all__ = ["SchedulerLock", "ScheduledRunResult", "run_scheduled"]
