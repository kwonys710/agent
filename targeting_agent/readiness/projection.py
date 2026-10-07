"""Action Policy Projection (Phase 18D.4).

LIVE를 켜면 하루에 **몇 건이** 실제로 나가는지 결정론적으로 추정한다.
Action을 만들지도, 실행하지도, 저장하지도 않는다 — 세기만 한다.

임계값 판단은 운영과 같은 `ActionPolicy`를 그대로 쓴다. 여기서 규칙을 다시
구현하면 "추정은 되는데 실제와 다른" 숫자가 나온다.

가드는 config 값을 보고 **순서대로** 적용한다.
    점수 하한 → Creator 미확인 → 이미 접촉한 게시물 → Creator 일일 한도
    → cooldown → 일일 총량
한 후보가 여러 이유로 막히면 **먼저 걸린 이유 하나만** 센다. 그래야 합계가 맞는다.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Optional, Sequence

from ..core.config import Config
from ..core.models import ActionType
from ..actions.policy import ActionPolicy

BLOCK_BELOW_MINIMUM = "below_minimum_score"
BLOCK_BELOW_THRESHOLD = "below_action_threshold"
BLOCK_UNRESOLVED = "creator_unresolved"
BLOCK_ALREADY = "already_interacted"
BLOCK_CREATOR_DAILY = "creator_daily_limit"
BLOCK_COOLDOWN = "creator_cooldown"
BLOCK_DAILY_LIMIT = "daily_limit"
BLOCK_NO_COMMENT = "no_comment_draft"

BROWSER_SOURCE = "instagram_browser_search"

_CANDIDATE_SQL = """
SELECT m.media_pk, m.media_id, m.creator_id, m.target_score, m.source,
       c.username, c.last_interacted_at,
       (SELECT COUNT(*) FROM interactions i WHERE i.media_pk = m.media_pk) AS interacted,
       (SELECT COUNT(*) FROM comment_drafts d
         WHERE d.media_pk = m.media_pk AND d.quality_ok = 1)               AS comment_drafts
FROM candidate_media m
LEFT JOIN creators c ON c.creator_id = m.creator_id
WHERE m.target_score IS NOT NULL
ORDER BY m.target_score DESC, m.media_pk
"""


@dataclass
class ProjectedCandidate:
    """후보 한 건이 어떤 Action으로 이어지는지."""

    media_id: str
    creator_id: int
    score: float
    likes: int = 0
    comments: int = 0
    blocked_by: str = ""

    @property
    def actions(self) -> int:
        return self.likes + self.comments


@dataclass
class Projection:
    """임계값 한 조합에 대한 추정 결과."""

    label: str
    like_threshold: float
    comment_threshold: float
    candidates: int = 0
    like_eligible: int = 0
    comment_eligible: int = 0
    projected_likes: int = 0
    projected_comments: int = 0
    blocked: dict[str, int] = field(default_factory=dict)
    good_target_overlap: int = 0
    not_my_style_overlap: int = 0
    # 자격을 얻은 후보가 **어느 수집 경로에서** 왔는지. Discovery가 실제로
    # Action으로 이어지고 있는지 보려면 총계만으로는 알 수 없다.
    eligible_by_source: dict[str, int] = field(default_factory=dict)

    def block(self, reason: str) -> None:
        self.blocked[reason] = self.blocked.get(reason, 0) + 1

    @property
    def projected_total(self) -> int:
        return self.projected_likes + self.projected_comments

    def as_row(self) -> dict[str, str]:
        return {
            "임계값": f"LIKE {self.like_threshold:.0f} / COMMENT {self.comment_threshold:.0f}",
            "후보": str(self.candidates),
            "자격(LIKE / COMMENT)": f"{self.like_eligible} / {self.comment_eligible}",
            "하루 예상(LIKE / COMMENT)": f"{self.projected_likes} / {self.projected_comments}",
            "자격 후보의 출처": ", ".join(
                f"{k}={v}" for k, v in sorted(self.eligible_by_source.items())
            )
            or "없음",
            "한도·가드로 보류": ", ".join(f"{k}={v}" for k, v in sorted(self.blocked.items()))
            or "없음",
            "사람 GOOD_TARGET 겹침": str(self.good_target_overlap),
            "사람 NOT_MY_STYLE 겹침": str(self.not_my_style_overlap),
        }


@dataclass
class ProjectionReport:
    """현재 설정 + 시나리오 비교."""

    current: Optional[Projection] = None
    scenarios: list[Projection] = field(default_factory=list)
    daily_like_limit: int = 0
    daily_comment_limit: int = 0
    ground_truth_available: bool = False
    notes: list[str] = field(default_factory=list)

    def as_rows(self) -> dict[str, str]:
        rows = {
            "하루 한도": f"LIKE {self.daily_like_limit} · COMMENT {self.daily_comment_limit}",
        }
        if self.current:
            for key, value in self.current.as_row().items():
                rows[f"현재 · {key}"] = value
        for scenario in self.scenarios:
            rows[f"시나리오 {scenario.label}"] = (
                f"자격 {scenario.like_eligible}/{scenario.comment_eligible} · "
                f"예상 {scenario.projected_likes}/{scenario.projected_comments}"
            )
        return rows


def _feedback_media(conn: sqlite3.Connection, feedback_type: str) -> set[int]:
    return {
        int(row[0])
        for row in conn.execute(
            "SELECT DISTINCT media_pk FROM feedback WHERE feedback_type = ?", (feedback_type,)
        )
    }


def _project_one(
    rows: Sequence[dict],
    policy: ActionPolicy,
    *,
    label: str,
    daily_like_limit: int,
    daily_comment_limit: int,
    max_per_creator_day: int,
    cooldown_days: int,
    skip_unresolved: bool,
    skip_already: bool,
    now: datetime,
    good_targets: set[int],
    bad_targets: set[int],
) -> Projection:
    projection = Projection(
        label=label,
        like_threshold=policy.like_threshold,
        comment_threshold=policy.comment_threshold,
        candidates=len(rows),
    )
    creator_actions: dict[int, int] = {}
    likes_left = daily_like_limit
    comments_left = daily_comment_limit

    for row in rows:
        score = float(row["target_score"])
        recommended = policy.recommend(score)
        if ActionType.LIKE in recommended:
            projection.like_eligible += 1
        if ActionType.COMMENT in recommended:
            projection.comment_eligible += 1
        if recommended:
            source = str(row["source"] or "(미상)")
            projection.eligible_by_source[source] = (
                projection.eligible_by_source.get(source, 0) + 1
            )

        if not recommended:
            projection.block(
                BLOCK_BELOW_MINIMUM if score < policy.minimum_score else BLOCK_BELOW_THRESHOLD
            )
            continue
        if skip_unresolved and str(row["username"] or "").startswith("unresolved:"):
            projection.block(BLOCK_UNRESOLVED)
            continue
        if skip_already and int(row["interacted"] or 0):
            projection.block(BLOCK_ALREADY)
            continue

        creator_id = int(row["creator_id"])
        if creator_actions.get(creator_id, 0) >= max_per_creator_day:
            projection.block(BLOCK_CREATOR_DAILY)
            continue
        if _in_cooldown(row["last_interacted_at"], cooldown_days=cooldown_days, now=now):
            projection.block(BLOCK_COOLDOWN)
            continue

        granted = 0
        if ActionType.LIKE in recommended:
            if likes_left > 0:
                likes_left -= 1
                projection.projected_likes += 1
                granted += 1
            else:
                projection.block(BLOCK_DAILY_LIMIT)
        if ActionType.COMMENT in recommended:
            if not int(row["comment_drafts"] or 0):
                projection.block(BLOCK_NO_COMMENT)
            elif comments_left > 0:
                comments_left -= 1
                projection.projected_comments += 1
                granted += 1
            else:
                projection.block(BLOCK_DAILY_LIMIT)

        if granted:
            # count_mode='media' 기준 — 같은 게시물의 LIKE+COMMENT는 1회 접촉이다.
            creator_actions[creator_id] = creator_actions.get(creator_id, 0) + 1
            media_pk = int(row["media_pk"])
            if media_pk in good_targets:
                projection.good_target_overlap += 1
            if media_pk in bad_targets:
                projection.not_my_style_overlap += 1
    return projection


def _in_cooldown(last_interacted_at, *, cooldown_days: int, now: datetime) -> bool:
    if not last_interacted_at or cooldown_days <= 0:
        return False
    text = str(last_interacted_at).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return False  # 못 읽는 값으로 막지 않는다(판정 불가는 가드 대상이 아니다)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return (now - parsed) < timedelta(days=cooldown_days)


def collect_projection(
    conn: sqlite3.Connection,
    config: Config,
    *,
    scenarios: Sequence[tuple[float, float]] = ((70.0, 80.0), (80.0, 85.0)),
    now: Optional[datetime] = None,
) -> ProjectionReport:
    """LIVE를 켰을 때의 하루 Action 수를 추정한다. **아무것도 실행·저장하지 않는다.**"""
    now = now or datetime.now(timezone.utc)
    cursor = conn.execute(_CANDIDATE_SQL)
    columns = [description[0] for description in cursor.description]
    rows = [dict(zip(columns, values)) for values in cursor.fetchall()]

    good_targets = _feedback_media(conn, "GOOD_TARGET")
    bad_targets = _feedback_media(conn, "NOT_MY_STYLE")

    common = {
        "daily_like_limit": int(config.get("actions.daily_limits.likes", 0)),
        "daily_comment_limit": int(config.get("actions.daily_limits.comments", 0)),
        "max_per_creator_day": int(config.get("actions.per_creator.max_actions_per_day", 1)),
        "cooldown_days": int(config.get("actions.per_creator.cooldown_days", 0)),
        "skip_unresolved": bool(config.get("safety.skip_unresolved_creator", True)),
        "skip_already": bool(config.get("safety.skip_if_already_interacted", True)),
        "now": now,
        "good_targets": good_targets,
        "bad_targets": bad_targets,
    }

    report = ProjectionReport(
        daily_like_limit=common["daily_like_limit"],
        daily_comment_limit=common["daily_comment_limit"],
        ground_truth_available=bool(good_targets or bad_targets),
    )

    policy = ActionPolicy.from_config(config)
    report.current = _project_one(rows, policy, label="현재", **common)

    for like_threshold, comment_threshold in scenarios:
        # ActionPolicy는 frozen이다 — 운영 정책 객체를 건드리지 않고 사본을 만든다.
        variant = replace(
            policy,
            like_threshold=float(like_threshold),
            comment_threshold=float(comment_threshold),
        )
        report.scenarios.append(
            _project_one(
                rows,
                variant,
                label=f"LIKE {like_threshold:.0f}/COMMENT {comment_threshold:.0f}",
                **common,
            )
        )

    if not report.ground_truth_available:
        report.notes.append(
            "사람이 남긴 GOOD_TARGET / NOT_MY_STYLE 신호가 없습니다 — "
            "시나리오는 분포만 보여 주고 어느 쪽이 낫다고 말하지 않습니다."
        )
    if report.current and report.current.projected_total == 0:
        report.notes.append(
            "현재 설정으로는 하루에 나갈 Action이 0건입니다 — LIVE로 바꿔도 아무 일도 생기지 않습니다."
        )
    if report.current:
        browser_eligible = report.current.eligible_by_source.get(BROWSER_SOURCE, 0)
        browser_total = sum(1 for row in rows if row["source"] == BROWSER_SOURCE)
        if browser_total and not browser_eligible:
            report.notes.append(
                f"Browser Discovery로 모은 후보 {browser_total}건 중 Action 자격을 얻은 건이 "
                "0건입니다 — 지금 수집 경로는 Action으로 이어지지 않습니다(18D.2의 점수 상한)."
            )
    report.notes.append(
        "'하루 예상'은 **지금 쌓여 있는 후보**를 하루 한도 안에서 처리했을 때의 수다 — "
        "매일 그만큼 나온다는 뜻이 아니다."
    )
    return report
