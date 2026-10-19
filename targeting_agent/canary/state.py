"""Limited Live Canary 상태 (Phase 18E).

Canary는 여러 번에 나눠 실행되고, 그 사이에 프로세스가 죽거나 PC가 재부팅될 수
있다. 그래서 "지금까지 실제로 몇 건을 눌렀는지"는 **파일에 남는다** — Scheduler나
메모리를 믿지 않는다.

이 파일이 마지막 방어선이다. Scheduler가 잘못 떠도, Task 삭제에 실패해도,
여기서 한도를 넘었다고 나오면 Runner는 아무 것도 하지 않는다.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

# 사용자가 Phase 18E에 한해 승인한 절대 상한. 코드에서 더 키울 수 없다.
MAX_TOTAL_LIKES = 6
MAX_LIKES_PER_DAY = 3
MAX_LIKES_PER_RUN = 2
CANARY_HOURS = 48
MIN_HOURS_BETWEEN_RUNS = 8

STATE_FILENAME = "live_canary_state.json"

NOT_STARTED = "NOT_STARTED"
ARMED = "ARMED"
# 18E.1 — 아직 실제로 누를 수 없는 이유를 상태로 구분한다.
# 둘 다 실패가 아니라 "기다리는 중"이다.
WAITING_AI_BUDGET = "WAITING_AI_BUDGET"      # Claude 일일 한도를 다 썼다
WAITING_ELIGIBLE = "WAITING_ELIGIBLE"        # 임계값을 넘는 실제 후보가 없다
READY_FOR_REHEARSAL = "READY_FOR_REHEARSAL"
READY_FOR_LIVE = "READY_FOR_LIVE"
RUNNING = "RUNNING"
WAITING_NEXT_RUN = "WAITING_NEXT_RUN"
COMPLETED = "COMPLETED"
EXPIRED_NO_ELIGIBLE = "EXPIRED_NO_ELIGIBLE"
STOPPED_SAFETY = "STOPPED_SAFETY"
STOPPED_WARNING = "STOPPED_WARNING"
STOPPED_CHALLENGE = "STOPPED_CHALLENGE"
STOPPED_ACTION_BLOCK = "STOPPED_ACTION_BLOCK"
STOPPED_LOGIN = "STOPPED_LOGIN"
STOPPED_UNKNOWN_WRITE = "STOPPED_UNKNOWN_WRITE"
STOPPED_VIOLATION = "STOPPED_VIOLATION"
FAILED = "FAILED"

# 더 이상 아무 것도 쓰지 않는 종료 상태.
TERMINAL_STATES = frozenset(
    {
        COMPLETED,
        EXPIRED_NO_ELIGIBLE,
        STOPPED_SAFETY,
        STOPPED_WARNING,
        STOPPED_CHALLENGE,
        STOPPED_ACTION_BLOCK,
        STOPPED_LOGIN,
        STOPPED_UNKNOWN_WRITE,
        STOPPED_VIOLATION,
        FAILED,
    }
)


# 기다리는 중일 뿐 끝난 것이 아닌 상태. Runner가 다시 진입해 이어간다.
WAITING_STATES = frozenset({WAITING_AI_BUDGET, WAITING_ELIGIBLE, WAITING_NEXT_RUN})


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _date_of(moment: datetime, *, offset_hours: int = 9) -> str:
    """운영 시간대 기준 날짜(일일 한도는 이 기준으로 센다)."""
    return (moment + timedelta(hours=offset_hours)).date().isoformat()


@dataclass
class CanaryState:
    """Canary 진행 상태. 파일 하나로 끝낸다(DB schema를 늘리지 않는다)."""

    canary_id: str = ""
    status: str = NOT_STARTED
    # created_at: Canary를 만든 때. 여기부터 48시간을 세면, 후보가 없어서
    # 기다리는 동안 창이 다 지나가 **한 번도 못 눌러 보고** 만료된다.
    # 그래서 48시간은 live_started_at(처음 실제로 누른 때)부터 센다(18E.1 §21).
    started_at: str = ""
    live_started_at: str = ""
    ends_at: str = ""
    last_run_at: str = ""
    last_waiting_reason: str = ""
    rehearsal_passed_at: str = ""
    discovery_runs: int = 0
    total_live_likes: int = 0
    today_live_likes: int = 0
    today_date: str = ""
    runs: int = 0
    stop_reason: str = ""
    completed_at: str = ""
    # 사람이 나중에 평가할 수 있도록 남기는 목록(Human GT를 자동으로 만들지 않는다).
    review_backlog: list[int] = field(default_factory=list)
    # 상한은 상태에 함께 적어 둔다 — 나중에 보고서만 보고도 무엇이 허용됐는지 안다.
    max_total: int = MAX_TOTAL_LIKES
    max_per_day: int = MAX_LIKES_PER_DAY
    max_per_run: int = MAX_LIKES_PER_RUN
    min_hours_between_runs: int = MIN_HOURS_BETWEEN_RUNS

    # --- 시간 ------------------------------------------------------------
    def is_expired(self, *, now: Optional[datetime] = None) -> bool:
        """창이 **열린 뒤에만** 만료를 따진다.

        한 번도 눌러 보지 못한 Canary는 만료될 수 없다 — 기다린 시간은
        쓰기 기간이 아니다.
        """
        if not self.live_started_at:
            return False
        ends = _parse(self.ends_at)
        return bool(ends and (now or _now()) >= ends)

    @property
    def expired(self) -> bool:
        return self.is_expired()

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL_STATES

    def next_allowed_run(self) -> Optional[datetime]:
        last = _parse(self.last_run_at)
        if last is None:
            return None
        return last + timedelta(hours=self.min_hours_between_runs)

    def interval_ok(self, *, now: Optional[datetime] = None) -> bool:
        allowed = self.next_allowed_run()
        return allowed is None or (now or _now()) >= allowed

    # --- 한도 ------------------------------------------------------------
    def roll_day(self, *, now: Optional[datetime] = None, offset_hours: int = 9) -> None:
        """날짜가 바뀌었으면 오늘 사용량을 0으로 되돌린다."""
        today = _date_of(now or _now(), offset_hours=offset_hours)
        if self.today_date != today:
            self.today_date = today
            self.today_live_likes = 0

    def remaining_total(self) -> int:
        return max(0, min(self.max_total, MAX_TOTAL_LIKES) - self.total_live_likes)

    def remaining_today(self) -> int:
        return max(0, min(self.max_per_day, MAX_LIKES_PER_DAY) - self.today_live_likes)

    def budget_for_run(self) -> int:
        """이번 Run에서 실제로 누를 수 있는 최대 건수.

        셋 중 **가장 작은 값**을 쓴다. 하나라도 0이면 아무 것도 하지 않는다.
        """
        return max(
            0,
            min(
                self.remaining_total(),
                self.remaining_today(),
                min(self.max_per_run, MAX_LIKES_PER_RUN),
            ),
        )

    def record_like(self, media_pk: int, *, now: Optional[datetime] = None) -> None:
        """실제 LIKE 1건을 기록한다. **확인된 건만** 넣는다."""
        moment = now or _now()
        self.roll_day(now=moment)
        self.total_live_likes += 1
        self.today_live_likes += 1
        if media_pk and media_pk not in self.review_backlog:
            self.review_backlog.append(int(media_pk))

    @property
    def live_window_open(self) -> bool:
        """실제 쓰기 창이 열렸는지. 열리기 전에는 만료도 없다."""
        return bool(self.live_started_at)

    def wait(self, status: str, reason: str) -> None:
        """기다리는 상태로 둔다. 끝난 것이 아니므로 종료 시각을 찍지 않는다."""
        self.status = status
        self.last_waiting_reason = reason

    def stop(self, status: str, reason: str, *, now: Optional[datetime] = None) -> None:
        self.status = status
        self.stop_reason = reason
        self.completed_at = (now or _now()).isoformat(timespec="seconds")

    def finish_run(self, *, now: Optional[datetime] = None) -> None:
        moment = now or _now()
        self.runs += 1
        self.last_run_at = moment.isoformat(timespec="seconds")
        if self.terminal:
            return
        if self.remaining_total() == 0 or self.is_expired(now=moment):
            self.status = COMPLETED
            self.stop_reason = "total_reached" if self.remaining_total() == 0 else "window_expired"
            self.completed_at = moment.isoformat(timespec="seconds")
        else:
            self.status = WAITING_NEXT_RUN

    def as_rows(self) -> dict[str, str]:
        next_run = self.next_allowed_run()
        return {
            "Canary ID": self.canary_id or "-",
            "상태": self.status,
            "생성": self.started_at or "-",
            "LIVE 창": (
                f"{self.live_started_at} ~ {self.ends_at}"
                if self.live_started_at
                else "아직 열리지 않음(첫 실제 시도 때 48시간 시작)"
            ),
            "대기 사유": self.last_waiting_reason or "-",
            "리허설 통과": self.rehearsal_passed_at or "-",
            "실제 LIKE (확인됨)": f"{self.total_live_likes} / {self.max_total}",
            "오늘": f"{self.today_live_likes} / {self.max_per_day} ({self.today_date or '-'})",
            "1회 상한": str(self.max_per_run),
            "Run 수": str(self.runs),
            "마지막 Run": self.last_run_at or "-",
            "다음 허용 시각": next_run.isoformat(timespec="seconds") if next_run else "즉시",
            "중단 사유": self.stop_reason or "-",
            "사람 검토 대기": str(len(self.review_backlog)),
        }


def state_path(data_dir: Path) -> Path:
    return Path(data_dir) / STATE_FILENAME


def load_state(path: Path) -> CanaryState:
    """상태를 읽는다. 파일이 깨졌으면 **새로 시작하지 않고** 안전하게 막는다."""
    if not path.exists():
        return CanaryState()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # 읽을 수 없는 상태 파일을 '처음부터'로 해석하면 한도를 다시 쓰게 된다.
        broken = CanaryState()
        broken.stop(FAILED, "state_file_unreadable")
        return broken
    known = {field_name for field_name in CanaryState.__dataclass_fields__}
    return CanaryState(**{k: v for k, v in raw.items() if k in known})


def save_state(path: Path, state: CanaryState) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(asdict(state), ensure_ascii=False, indent=2), encoding="utf-8"
    )


def arm(state: CanaryState, *, now: Optional[datetime] = None) -> CanaryState:
    """Canary를 만든다. **48시간 창은 아직 열지 않는다.**

    후보가 없거나 AI 한도가 비어 기다리는 동안 창이 흘러가면, 정작 누를 수
    있게 됐을 때 이미 만료돼 있다. 창은 `start_live_window()`에서 연다.
    """
    moment = now or _now()
    if state.status != NOT_STARTED:
        return state
    state.canary_id = moment.strftime("canary-%Y%m%d-%H%M%S")
    state.status = ARMED
    state.started_at = moment.isoformat(timespec="seconds")
    state.roll_day(now=moment)
    return state


def start_live_window(state: CanaryState, *, now: Optional[datetime] = None) -> CanaryState:
    """**처음 실제로 누르려는 순간** 48시간 창을 연다(한 번만)."""
    if state.live_started_at:
        return state
    moment = now or _now()
    state.live_started_at = moment.isoformat(timespec="seconds")
    state.ends_at = (moment + timedelta(hours=CANARY_HOURS)).isoformat(timespec="seconds")
    return state
