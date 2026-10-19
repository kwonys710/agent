"""Action Policy — Target Score로 어떤 Action을 추천할지 한 곳에서 정한다 (Phase 18B.1).

임계값은 이미 `config.yaml`에 있다(`actions.require_score_for_like` /
`actions.require_score_for_comment`). 이 모듈은 그 값을 **다시 정의하지 않고**
"점수 → 추천 Action" 판단만 한 곳으로 모은다. Queue 생성, Dashboard 기본 체크,
Autopilot 자동 승인이 모두 같은 규칙을 쓰게 하기 위함이다.

규칙:
    score < minimum_target_score          → 없음
    score < require_score_for_like        → 없음
    like <= score < comment               → LIKE
    score >= require_score_for_comment    → LIKE + COMMENT (댓글 후보가 있을 때)

임계값은 **추천 기준**이며 차단이 아니다. 운영자는 Dashboard에서 직접 override할 수 있다.
Agent는 이 임계값을 스스로 바꾸지 않는다(학습은 추천까지만 — Phase 17 원칙).
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Sequence

from ..core.config import Config
from ..core.database import APPROVED_BY_AUTOPILOT, approve_action
from ..core.logger import get_logger
from ..core.models import ActionType

logger = get_logger("actions.policy")


class RunMode(str, Enum):
    """운용 모드. 두 모드를 섞지 않는다."""

    REVIEW = "REVIEW"        # 운영자가 Dashboard에서 승인한다(기존 방식, 기본값)
    AUTOPILOT = "AUTOPILOT"  # Policy 기준을 만족한 Action을 자동 승인한다

    @classmethod
    def from_config(cls, config: Config) -> "RunMode":
        return cls.AUTOPILOT if bool(config.get("autopilot.enabled", False)) else cls.REVIEW


@dataclass(frozen=True)
class ActionPolicy:
    """점수 → 추천 Action. config 임계값을 그대로 읽어 쓴다."""

    minimum_score: float
    like_threshold: float
    comment_threshold: float
    enable_like: bool = True
    enable_comment: bool = True

    @classmethod
    def from_config(cls, config: Config) -> "ActionPolicy":
        return cls(
            minimum_score=float(config.get("scoring.minimum_target_score", 70)),
            like_threshold=float(config.get("actions.require_score_for_like", 75)),
            comment_threshold=float(config.get("actions.require_score_for_comment", 82)),
            enable_like=bool(config.get("actions.enable_like", True)),
            enable_comment=bool(config.get("actions.enable_comment", True)),
        )

    def recommend(
        self, score: Optional[float], *, apply_minimum: bool = True
    ) -> tuple[ActionType, ...]:
        """추천 Action 목록(없으면 빈 tuple).

        `apply_minimum=False`는 Dashboard의 기본 체크 표시에만 쓴다. 운영자가 임계값을
        `scoring.minimum_target_score`보다 낮게 내렸을 때, 화면에는 운영자가 설정한
        기준대로 보여 주기 위함이다. 자동 경로(Queue 생성/자동 승인)는 항상 하한을 지킨다.
        """
        if score is None:
            return ()
        if apply_minimum and float(score) < self.minimum_score:
            return ()
        value = float(score)
        actions: list[ActionType] = []
        if self.enable_like and value >= self.like_threshold:
            actions.append(ActionType.LIKE)
        if self.enable_comment and value >= self.comment_threshold:
            actions.append(ActionType.COMMENT)
        return tuple(actions)

    def allows(self, action_type: ActionType, score: Optional[float]) -> bool:
        return action_type in self.recommend(score)

    def explain(self, score: Optional[float]) -> str:
        """운영자에게 보여 줄 한 줄 설명(Dashboard/Summary 공용)."""
        text = "-" if score is None else f"{float(score):.0f}"
        recommended = self.recommend(score)
        names = " + ".join(a.value for a in recommended) if recommended else "없음"
        return (
            f"Score {text} · 추천 {names} "
            f"(LIKE {self.like_threshold:.0f} / COMMENT {self.comment_threshold:.0f})"
        )


# --- Comment Eligibility / Action Mix (Phase 19F·19I) ---------------------
COMMENT_OK = "OK"
COMMENT_BELOW_THRESHOLD = "below_comment_threshold"
COMMENT_NO_CAPTION = "no_caption"
COMMENT_NO_CANDIDATE = "no_comment_candidate"
COMMENT_QUALITY_FAIL = "quality_fail"
COMMENT_DUPLICATE = "duplicate_comment"
COMMENT_DISABLED = "comment_disabled"
COMMENT_RECENT_SAME_CREATOR = "recent_comment_same_creator"


@dataclass
class CommentEligibility:
    """COMMENT 가능 여부. 실패해도 LIKE는 살린다(19I Action Mix)."""

    ok: bool
    reason: str = COMMENT_OK
    draft_id: Optional[int] = None
    text: Optional[str] = None

    @property
    def quality_pass(self) -> bool:
        return self.ok


def comment_eligibility(
    conn: sqlite3.Connection,
    config: Config,
    media_pk: int,
    score: Optional[float],
    *,
    policy: Optional[ActionPolicy] = None,
) -> CommentEligibility:
    """저장된 댓글 후보를 기준으로 COMMENT 가능 여부를 판단한다.

    댓글을 **새로 만들지 않는다** — Phase 4의 생성·품질·중복 필터가 이미 통과시킨
    comment_drafts만 본다(quality_ok=1). 기준 점수는 config가 single source다.
    """
    rules = policy or ActionPolicy.from_config(config)
    if not rules.enable_comment:
        return CommentEligibility(False, COMMENT_DISABLED)
    if ActionType.COMMENT not in rules.recommend(score):
        return CommentEligibility(False, COMMENT_BELOW_THRESHOLD)

    media = conn.execute(
        "SELECT caption, creator_id FROM candidate_media WHERE media_pk = ?", (int(media_pk),)
    ).fetchone()
    if media is None or not str(media["caption"] or "").strip():
        # caption이 없으면 맥락 없는 댓글이 된다 → COMMENT 제외(LIKE는 가능).
        return CommentEligibility(False, COMMENT_NO_CAPTION)

    draft = conn.execute(
        "SELECT draft_id, text FROM comment_drafts "
        "WHERE media_pk = ? AND quality_ok = 1 AND status != 'REJECTED' "
        "ORDER BY (status = 'SELECTED') DESC, COALESCE(similarity_max, 0) ASC, draft_id ASC "
        "LIMIT 1",
        (int(media_pk),),
    ).fetchone()
    if draft is None:
        # 생성 실패/품질 탈락 구분: 후보 행이 아예 없으면 생성 실패로 본다.
        any_draft = conn.execute(
            "SELECT COUNT(*) AS n FROM comment_drafts WHERE media_pk = ?", (int(media_pk),)
        ).fetchone()
        reason = COMMENT_QUALITY_FAIL if int(any_draft["n"] or 0) else COMMENT_NO_CANDIDATE
        return CommentEligibility(False, reason)

    cooldown_hours = int(config.get("comments.same_creator_cooldown_hours", 0))
    if cooldown_hours and _recent_creator_comment(conn, int(media["creator_id"]), cooldown_hours):
        return CommentEligibility(False, COMMENT_RECENT_SAME_CREATOR)

    return CommentEligibility(True, COMMENT_OK, int(draft["draft_id"]), str(draft["text"]))


def _recent_creator_comment(conn: sqlite3.Connection, creator_id: int, hours: int) -> bool:
    """같은 Creator에게 최근에 댓글을 달았는지(기존 interactions 기록만 본다)."""
    row = conn.execute(
        "SELECT 1 FROM interactions "
        "WHERE creator_id = ? AND action_type = 'COMMENT' AND success = 1 AND dry_run = 0 "
        "  AND executed_at >= datetime('now', ?) LIMIT 1",
        (int(creator_id), f"-{int(hours)} hours"),
    ).fetchone()
    return row is not None


@dataclass
class ActionMix:
    """후보 1건에 대해 실제로 만들 Action 조합(19I)."""

    actions: tuple[ActionType, ...] = ()
    comment: CommentEligibility = field(
        default_factory=lambda: CommentEligibility(False, COMMENT_BELOW_THRESHOLD)
    )
    reason: str = ""

    @property
    def has_like(self) -> bool:
        return ActionType.LIKE in self.actions

    @property
    def has_comment(self) -> bool:
        return ActionType.COMMENT in self.actions


def decide_action_mix(
    conn: sqlite3.Connection,
    config: Config,
    media_pk: int,
    score: Optional[float],
    *,
    policy: Optional[ActionPolicy] = None,
) -> ActionMix:
    """Score + Comment 품질로 Action 조합을 정한다.

        Score < LIKE 기준                      → 없음
        LIKE 기준 ≤ Score < COMMENT 기준       → LIKE
        Score ≥ COMMENT 기준 + 댓글 품질 실패  → LIKE (댓글만 빼고 진행)
        Score ≥ COMMENT 기준 + 댓글 품질 통과  → LIKE + COMMENT
    """
    rules = policy or ActionPolicy.from_config(config)
    recommended = rules.recommend(score)
    if not recommended:
        return ActionMix((), CommentEligibility(False, COMMENT_BELOW_THRESHOLD), "below_threshold")

    eligibility = comment_eligibility(conn, config, media_pk, score, policy=rules)
    if ActionType.COMMENT in recommended and eligibility.ok:
        return ActionMix((ActionType.LIKE, ActionType.COMMENT), eligibility, "like_and_comment")
    if ActionType.COMMENT in recommended:
        return ActionMix((ActionType.LIKE,), eligibility, f"like_only:{eligibility.reason}")
    return ActionMix((ActionType.LIKE,), eligibility, "like_only")


@dataclass
class AutoApproveResult:
    """자동 승인 결과."""

    mode: str = RunMode.REVIEW.value
    approved: int = 0
    skipped: int = 0
    reasons: dict[str, int] = field(default_factory=dict)
    approved_ids: list[int] = field(default_factory=list)

    def note(self, reason: str) -> None:
        self.reasons[reason] = self.reasons.get(reason, 0) + 1


def pending_unapproved(conn: sqlite3.Connection, limit: Optional[int] = None) -> list[sqlite3.Row]:
    """아직 승인되지 않은 PENDING Action(점수·Creator 정보 포함)."""
    sql = (
        "SELECT a.action_id, a.media_pk, a.action_type, a.target_score, a.comment_text, "
        "       c.username "
        "FROM action_queue a "
        "JOIN candidate_media m ON m.media_pk = a.media_pk "
        "JOIN creators c ON c.creator_id = a.creator_id "
        "WHERE a.status = 'PENDING' AND a.approved_at IS NULL "
        "ORDER BY a.priority DESC, a.action_id ASC"
    )
    if limit:
        sql += f" LIMIT {int(limit)}"
    return list(conn.execute(sql).fetchall())


def auto_approve(
    conn: sqlite3.Connection,
    config: Config,
    *,
    mode: Optional[RunMode] = None,
    policy: Optional[ActionPolicy] = None,
    limit: Optional[int] = None,
) -> AutoApproveResult:
    """AUTOPILOT 모드에서 Policy 기준을 만족한 Action만 자동 승인한다.

    REVIEW 모드에서는 **아무 것도 승인하지 않는다**(Approval Gate 유지).
    승인은 approved_at만 채운다 — 실행은 기존 Executor가 한다.
    """
    run_mode = mode or RunMode.from_config(config)
    result = AutoApproveResult(mode=run_mode.value)
    if run_mode is not RunMode.AUTOPILOT:
        logger.info("REVIEW 모드 — 자동 승인하지 않습니다(운영자 승인 필요).")
        return result

    rules = policy or ActionPolicy.from_config(config)
    cap = limit if limit is not None else int(config.get("autopilot.max_approvals_per_run", 10))
    rows = pending_unapproved(conn)

    for row in rows:
        if cap and result.approved >= cap:
            result.skipped += 1
            result.note("approval_cap")
            continue

        action_type = ActionType(str(row["action_type"]))
        score = row["target_score"]

        if str(row["username"] or "").startswith("unresolved:"):
            result.skipped += 1
            result.note("creator_unresolved")
            continue
        if not rules.allows(action_type, score):
            result.skipped += 1
            result.note(f"below_threshold_{action_type.value.lower()}")
            continue
        if action_type is ActionType.COMMENT and not str(row["comment_text"] or "").strip():
            result.skipped += 1
            result.note("no_comment_text")
            continue

        if approve_action(conn, int(row["action_id"]), by=APPROVED_BY_AUTOPILOT):
            result.approved += 1
            result.approved_ids.append(int(row["action_id"]))
        else:
            result.skipped += 1
            result.note("already_approved")

    conn.commit()
    logger.info(
        "Autopilot 자동 승인: %d건 승인 / %d건 보류 (%s)",
        result.approved,
        result.skipped,
        ", ".join(f"{k}={v}" for k, v in sorted(result.reasons.items())) or "-",
    )
    return result


def usage_note(config: Config, conn: sqlite3.Connection) -> str:
    """오늘 사용량 한 줄 요약. 기존 RateLimiter 집계를 그대로 쓴다(중복 계산 금지)."""
    from .rate_limiter import RateLimiter

    usage = RateLimiter(config, conn).usage_summary()
    return " · ".join(
        f"{action_type} {used}/{limit}" for action_type, (used, limit) in sorted(usage.items())
    )


def recommended_for(policy: ActionPolicy, scores: Sequence[Optional[float]]) -> dict[str, int]:
    """점수 분포에 대한 추천 분포(모니터링용)."""
    counts = {"none": 0, "like": 0, "like_comment": 0}
    for score in scores:
        actions = policy.recommend(score)
        if not actions:
            counts["none"] += 1
        elif ActionType.COMMENT in actions:
            counts["like_comment"] += 1
        else:
            counts["like"] += 1
    return counts
