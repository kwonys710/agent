"""Pre-Live Guardrails (Phase 18D.5).

LIVE로 넘어가기 전에 "실제 쓰기를 막는 장치가 정말 켜져 있는지"를 확인한다.
설정을 **읽기만** 한다 — 아무것도 바꾸지 않는다.

가장 중요한 규칙 하나:

    실제 Instagram 쓰기는 **두 스위치가 모두 풀려야** 일어난다.
        browser_executor.mode = LIVE   그리고   actions.execution.dry_run = false
    하나라도 안전 상태면 쓰기는 없다.

설정이 서로 모순될 수 있다(LIVE인데 dry_run=true). 그때 "무엇을 설정했는지"와
"실제로 무엇이 일어나는지"를 따로 보여 준다 — 둘을 한 칸에 적으면 운영자가
LIVE라고 믿고 있는데 아무 일도 안 일어나거나, 그 반대가 된다.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Optional

from ..core.config import Config

PASS = "PASS"
WARN = "WARN"
FAIL = "FAIL"

EFFECTIVE_LIVE = "LIVE"
EFFECTIVE_DRY_RUN = "DRY_RUN"
EFFECTIVE_DISABLED = "DISABLED"


@dataclass(frozen=True)
class EffectiveMode:
    """설정값과 실제 동작을 따로 담는다."""

    configured: str
    effective: str
    reason: str = ""

    @property
    def writes_enabled(self) -> bool:
        return self.effective == EFFECTIVE_LIVE

    def as_line(self) -> str:
        line = f"Configured: {self.configured} / Effective: {self.effective}"
        return f"{line} (Reason: {self.reason})" if self.reason else line


def effective_mode(config: Config) -> EffectiveMode:
    """실제로 Instagram에 쓰는 상태인지 판정한다.

    두 스위치가 모두 풀려야 LIVE다. 알 수 없는 mode 값은 가장 안전한 쪽으로 본다.
    """
    configured = str(config.get("browser_executor.mode", EFFECTIVE_DRY_RUN) or "").strip().upper()
    dry_run = bool(config.get("actions.execution.dry_run", True))

    if configured == EFFECTIVE_DISABLED:
        return EffectiveMode(configured, EFFECTIVE_DISABLED, "browser_executor.mode=DISABLED")
    if configured not in (EFFECTIVE_LIVE, EFFECTIVE_DRY_RUN):
        return EffectiveMode(
            configured or "(없음)",
            EFFECTIVE_DRY_RUN,
            f"알 수 없는 browser_executor.mode={configured or '(없음)'} — 안전한 쪽으로 처리",
        )
    if configured == EFFECTIVE_LIVE and dry_run:
        return EffectiveMode(
            EFFECTIVE_LIVE, EFFECTIVE_DRY_RUN, "actions.execution.dry_run=true"
        )
    if configured == EFFECTIVE_LIVE:
        return EffectiveMode(EFFECTIVE_LIVE, EFFECTIVE_LIVE)
    return EffectiveMode(EFFECTIVE_DRY_RUN, EFFECTIVE_DRY_RUN)


@dataclass
class GuardCheck:
    """안전장치 한 항목."""

    name: str
    status: str
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.status != FAIL


@dataclass
class GuardReport:
    """Pre-Live 안전 점검 결과."""

    mode: EffectiveMode = field(
        default_factory=lambda: EffectiveMode(EFFECTIVE_DRY_RUN, EFFECTIVE_DRY_RUN)
    )
    checks: list[GuardCheck] = field(default_factory=list)

    def add(self, name: str, status: str, detail: str = "") -> None:
        self.checks.append(GuardCheck(name=name, status=status, detail=detail))

    @property
    def failures(self) -> list[GuardCheck]:
        return [check for check in self.checks if check.status == FAIL]

    @property
    def warnings(self) -> list[GuardCheck]:
        return [check for check in self.checks if check.status == WARN]

    @property
    def passed(self) -> bool:
        return not self.failures

    def as_rows(self) -> dict[str, str]:
        rows = {"Executor 모드": self.mode.as_line()}
        for check in self.checks:
            rows[check.name] = f"{check.status}{(' — ' + check.detail) if check.detail else ''}"
        return rows


def collect_guards(config: Config, conn: Optional[sqlite3.Connection] = None) -> GuardReport:
    """LIVE 전에 켜져 있어야 할 장치를 확인한다. **설정을 바꾸지 않는다.**"""
    report = GuardReport(mode=effective_mode(config))
    mode = report.mode

    # --- 이중 스위치 ------------------------------------------------------
    if mode.writes_enabled:
        report.add(
            "이중 안전 스위치",
            WARN,
            "두 스위치가 모두 풀려 있습니다 — 실제 Instagram 쓰기가 일어납니다.",
        )
    else:
        report.add("이중 안전 스위치", PASS, f"실제 쓰기 없음 ({mode.as_line()})")

    # --- Autopilot / Scheduler -------------------------------------------
    autopilot = bool(config.get("autopilot.enabled", False))
    scheduler_autopilot = bool(config.get("scheduler.autopilot", False))
    report.add(
        "Autopilot 자동 승인",
        WARN if autopilot else PASS,
        "autopilot.enabled=true — Policy를 만족한 Action이 자동 승인됩니다."
        if autopilot
        else "autopilot.enabled=false — 운영자 승인 없이는 실행되지 않습니다.",
    )
    # scheduler.autopilot 하나만으로 쓰기가 열려서는 안 된다.
    report.add(
        "Scheduler LIVE 가드",
        PASS if not mode.writes_enabled else WARN,
        "scheduler.autopilot=true 라도 Executor가 안전 상태면 쓰기는 없습니다."
        if not mode.writes_enabled
        else f"scheduler.autopilot={str(scheduler_autopilot).lower()} + Executor LIVE — 자동 실행됩니다.",
    )

    # --- 한도 ------------------------------------------------------------
    for label, key in (
        ("하루 LIKE 한도", "actions.daily_limits.likes"),
        ("하루 COMMENT 한도", "actions.daily_limits.comments"),
        ("Creator 하루 한도", "actions.per_creator.max_actions_per_day"),
        ("Creator cooldown(일)", "actions.per_creator.cooldown_days"),
        ("Run당 최대 Action", "actions.execution.max_actions_per_run"),
    ):
        value = int(config.get(key, 0) or 0)
        report.add(
            label,
            PASS if value > 0 else FAIL,
            str(value) if value > 0 else "0 — 상한이 없으면 사고가 나도 멈추지 않습니다.",
        )

    # --- 중단 조건 --------------------------------------------------------
    for label, key in (
        ("비공개 계정 제외", "safety.skip_private_accounts"),
        ("광고 제외", "safety.skip_ads"),
        ("민감 콘텐츠 제외", "safety.skip_sensitive_content"),
        ("중복 접촉 차단", "safety.skip_if_already_interacted"),
        ("Creator 미확인 제외", "safety.skip_unresolved_creator"),
        ("중복 댓글 차단", "safety.block_duplicate_comments"),
        ("플랫폼 경고 시 중단", "safety.halt_on_platform_warning"),
    ):
        enabled = bool(config.get(key, False))
        report.add(label, PASS if enabled else FAIL, "켜짐" if enabled else f"{key}=false")

    # --- 댓글 품질 --------------------------------------------------------
    for label, key in (
        ("일반적인 댓글 거르기", "comments.avoid_generic_comments"),
        ("caption 근거 요구", "comments.require_context_keyword"),
    ):
        enabled = bool(config.get(key, False))
        report.add(label, PASS if enabled else WARN, "켜짐" if enabled else f"{key}=false")

    threshold = float(config.get("comments.duplicate_similarity_threshold", 0) or 0)
    report.add(
        "중복 댓글 유사도 기준",
        PASS if 0 < threshold < 1 else FAIL,
        f"{threshold}" if 0 < threshold < 1 else "0~1 사이여야 합니다.",
    )

    # --- Lock -------------------------------------------------------------
    for label, key in (
        ("Discovery Lock", "browser_discovery.lock.enabled"),
        ("Autopilot Lock", "autopilot.lock.enabled"),
        ("Scheduler Lock", "scheduler.lock.enabled"),
    ):
        enabled = bool(config.get(key, False))
        report.add(label, PASS if enabled else WARN, "켜짐" if enabled else f"{key}=false")

    # --- 실제 기록 확인 ---------------------------------------------------
    if conn is not None:
        real_writes = int(
            conn.execute(
                "SELECT COUNT(*) FROM interactions WHERE dry_run = 0 AND success = 1"
            ).fetchone()[0]
        )
        report.add(
            "실제 Instagram 쓰기 기록",
            PASS if real_writes == 0 else WARN,
            f"{real_writes}건",
        )
    return report
