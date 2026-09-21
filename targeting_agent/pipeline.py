"""Targeting Pipeline.

Discovery → 중복 제거 → 저장 → 분석 → Score → 필터 → 댓글 생성 →
Action Queue → Rate Limit → Executor → Interaction 기록 → 통계 → Summary

각 단계는 성공했을 때만 상태를 전이하므로, 중간에 종료되어도
다시 실행하면 남은 상태(NEW/ANALYZED/SCORED/QUEUED)부터 이어서 처리한다.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Sequence

from .actions.controller import ActionController, ExecutionSummary
from .actions.executors import create_executor
from .actions.queue import ActionQueueBuilder, QueueBuildResult, merge_results
from .actions.rate_limiter import RateLimiter
from .analysis.content_analyzer import ContentAnalyzer
from .analysis.profile_analyzer import TargetProfile, load_profile
from .analysis.scorer import TargetScorer, disqualify_reason, persist_score
from .comments.duplicate_filter import CommentDuplicateFilter
from .comments.generator import CommentGenerator
from .core.config import Config
from .core.database import (
    bump_stat,
    fetch_candidates,
    get_state,
    insert_candidate,
    save_comment_draft,
    set_state,
    today_str,
    transaction,
    update_candidate_status,
)
from .core.logger import get_logger
from .core.models import DraftStatus, MediaStatus, RawCandidate
from .discovery.base import dedupe_candidates, filter_by_config
from .discovery.hashtag_discovery import HashtagDiscovery
from .discovery.import_source import ImportDiscovery
from .discovery.ingest import CandidateIngestor, ImportSummary
from .learning.profile_optimizer import load_weights_override

logger = get_logger("pipeline")


@dataclass
class DiscoveryStats:
    found: int = 0
    duplicate: int = 0
    filtered: int = 0
    new: int = 0
    filter_reasons: dict[str, int] = field(default_factory=dict)


@dataclass
class AnalysisStats:
    analyzed: int = 0
    cached: int = 0
    disqualified: int = 0
    above_minimum: int = 0
    above_auto: int = 0
    errors: int = 0


@dataclass
class CommentStats:
    posts: int = 0
    generated: int = 0
    rejected_quality: int = 0
    rejected_duplicate: int = 0
    none_generated: int = 0


@dataclass
class RunSummary:
    run_id: str
    dry_run: bool
    executor: str
    analyzer: str
    discovery: DiscoveryStats = field(default_factory=DiscoveryStats)
    analysis: AnalysisStats = field(default_factory=AnalysisStats)
    comments: CommentStats = field(default_factory=CommentStats)
    queue: QueueBuildResult = field(default_factory=QueueBuildResult)
    execution: ExecutionSummary = field(default_factory=ExecutionSummary)
    limits: dict[str, tuple[int, int]] = field(default_factory=dict)
    export_path: Optional[str] = None
    ingest: Optional[ImportSummary] = None
    profile_source: str = "file"
    weights_source: str = "config"


class TargetingPipeline:
    """설정과 DB 연결을 받아 전체 파이프라인을 실행한다."""

    def __init__(
        self,
        config: Config,
        conn: sqlite3.Connection,
        *,
        run_id: str,
        profile: Optional[TargetProfile] = None,
        seed: Optional[int] = None,
    ) -> None:
        self.config = config
        self.conn = conn
        self.run_id = run_id
        self.profile = profile or load_profile(config.profile_path, conn)
        self.analyzer = ContentAnalyzer(config, self.profile)
        # 학습으로 조정된 가중치가 있으면 그것을 사용한다(app_state.scoring_weights).
        self.weights_override = load_weights_override(conn)
        self.scorer = TargetScorer(config, self.profile, self.weights_override)
        self.comment_generator = CommentGenerator(config, self.profile, seed=seed)
        self.tz_offset = int(config.get("actions.daily_limits.timezone_offset_hours", 9))
        self.date = today_str(self.tz_offset)
        self.summary = RunSummary(
            run_id=run_id,
            dry_run=config.dry_run,
            executor=config.executor_mode,
            analyzer=self.analyzer.active_name,
            profile_source="db(학습 반영)" if self.profile.source == "db" else "file",
            weights_source=self.scorer.weights_source,
        )

    # --- Phase 2: Discovery ---------------------------------------------
    def ingest_inbox(self) -> Optional[ImportSummary]:
        """data/inbox/*.csv 를 후보로 등록한다(Phase 12A)."""
        if not bool(self.config.get("discovery.inbox.enabled", True)):
            return None
        inbox = self.config._resolve_path(self.config.get("discovery.inbox.path", "data/inbox"))
        summary = CandidateIngestor(self.conn).process_inbox(
            inbox,
            processed_dir=self.config._resolve_path(
                self.config.get("discovery.inbox.processed_dir", "data/inbox/processed")
            ),
            failed_dir=self.config._resolve_path(
                self.config.get("discovery.inbox.failed_dir", "data/inbox/failed")
            ),
        )
        if summary.has_input:
            self.summary.ingest = summary
            self.summary.discovery.found += summary.input_count
            self.summary.discovery.new += summary.added
            self.summary.discovery.duplicate += summary.duplicate
        return summary

    def discover(self, source_path: Optional[Path | str] = None) -> DiscoveryStats:
        stats = self.summary.discovery
        if not bool(self.config.get("discovery.enabled", True)):
            logger.info("Discovery가 비활성화되어 있습니다.")
            return stats

        candidates = self._collect(source_path)
        stats.found = len(candidates)

        candidates, in_run_duplicates = dedupe_candidates(candidates)
        stats.duplicate += in_run_duplicates

        kept, reasons = filter_by_config(
            candidates,
            languages=[str(l) for l in self.config.list_of("discovery.languages")],
            content_types=[str(t) for t in self.config.list_of("discovery.content_types")],
            min_followers=int(self.config.get("discovery.creator.min_followers", 0)),
            max_followers=int(self.config.get("discovery.creator.max_followers", 10**9)),
            skip_private=bool(self.config.get("safety.skip_private_accounts", True)),
            skip_ads=bool(self.config.get("safety.skip_ads", True)),
        )
        stats.filtered = len(candidates) - len(kept)
        stats.filter_reasons = reasons

        with transaction(self.conn):
            for candidate in kept:
                media_pk = insert_candidate(self.conn, candidate)
                if media_pk is None:
                    stats.duplicate += 1  # 이미 DB에 있는 media_id
                    continue
                stats.new += 1
            bump_stat(self.conn, self.date, "discovered", stats.new)

        logger.info(
            "Discovery 완료: found=%d duplicate=%d filtered=%d new=%d",
            stats.found,
            stats.duplicate,
            stats.filtered,
            stats.new,
        )
        return stats

    def _collect(self, source_path: Optional[Path | str]) -> list[RawCandidate]:
        max_candidates = int(self.config.get("discovery.max_candidates_per_run", 200))
        source_name = str(self.config.get("discovery.default_source", "import"))

        if source_path or source_name == "import":
            path = Path(source_path) if source_path else self.config._resolve_path(
                self.config.get("discovery.import.path", "samples/candidates_sample.csv")
            )
            return ImportDiscovery(path).discover(limit=max_candidates)

        if source_name == "hashtag":
            hashtags = [str(h) for h in self.config.list_of("discovery.hashtags")]
            discovery = HashtagDiscovery(
                hashtags,
                edge=str(self.config.get("discovery.hashtag.edge", "recent_media")),
                per_hashtag_limit=int(self.config.get("discovery.hashtag.per_hashtag_limit", 25)),
                max_hashtags_per_run=int(
                    self.config.get("discovery.hashtag.max_hashtags_per_run", 5)
                ),
                media_types=[str(t) for t in self.config.list_of("discovery.content_types")],
                conn=self.conn,
            )
            return discovery.discover(limit=max_candidates)

        logger.warning("알 수 없는 discovery.default_source: %s", source_name)
        return []

    # --- Phase 3~4: 분석 / Score / 댓글 ----------------------------------
    def analyze_and_score(self) -> AnalysisStats:
        stats = self.summary.analysis
        comment_stats = self.summary.comments
        queue_builder = ActionQueueBuilder(self.config, self.run_id)
        duplicate_filter = CommentDuplicateFilter.from_db(self.config, self.conn)
        comments_enabled = bool(self.config.get("comments.enabled", True))
        minimum = float(self.config.get("scoring.minimum_target_score", 70))
        auto = float(self.config.get("scoring.auto_action_score", minimum))
        comment_threshold = float(self.config.get("actions.require_score_for_comment", auto))
        max_media = int(self.config.get("analysis.max_media_per_run", 100))

        rows = fetch_candidates(
            self.conn,
            [MediaStatus.NEW.value, MediaStatus.ANALYZED.value, MediaStatus.SCORED.value],
            limit=max_media,
        )
        logger.info("분석 대상 후보 %d건", len(rows))

        queue_results: list[QueueBuildResult] = []
        for row in rows:
            media = dict(row)
            media_pk = int(media["media_pk"])
            try:
                analysis, cached = self.analyzer.analyze_media(self.conn, media)
                if cached:
                    stats.cached += 1
                stats.analyzed += 1

                breakdown = self.scorer.score(media, analysis)
                persist_score(self.conn, media_pk, analysis, breakdown)

                reason = disqualify_reason(self.config, media, analysis, breakdown)
                if reason:
                    stats.disqualified += 1
                    update_candidate_status(
                        self.conn, media_pk, MediaStatus.BLOCKED, reason, breakdown.total
                    )
                    self.conn.commit()
                    continue

                score = breakdown.total
                if score >= minimum:
                    stats.above_minimum += 1
                if score >= auto:
                    stats.above_auto += 1
                update_candidate_status(
                    self.conn, media_pk, MediaStatus.SCORED, "scored", score
                )

                draft_id: Optional[int] = None
                comment_text: Optional[str] = None
                if comments_enabled and score >= comment_threshold:
                    draft_id, comment_text = self._generate_comments(
                        media, analysis, duplicate_filter, comment_stats
                    )

                queue_results.append(
                    queue_builder.build(self.conn, media, score, draft_id, comment_text)
                )
                self.conn.commit()
            except Exception as exc:  # noqa: BLE001 - 후보 단위로 오류 격리
                stats.errors += 1
                logger.exception("후보 처리 실패: media_id=%s", media.get("media_id"))
                update_candidate_status(
                    self.conn, media_pk, MediaStatus.ERROR, str(exc)[:200]
                )
                self.conn.commit()

        self.summary.queue = merge_results(queue_results)
        with transaction(self.conn):
            bump_stat(self.conn, self.date, "analyzed", stats.analyzed)
            bump_stat(self.conn, self.date, "queued", self.summary.queue.total)
        return stats

    def _generate_comments(
        self,
        media: dict[str, Any],
        analysis: Any,
        duplicate_filter: CommentDuplicateFilter,
        stats: CommentStats,
    ) -> tuple[Optional[int], Optional[str]]:
        """댓글 후보를 생성/저장하고 선택된 댓글을 반환한다."""
        candidates = self.comment_generator.generate(media, analysis, duplicate_filter)
        stats.posts += 1

        selected_id: Optional[int] = None
        selected_text: Optional[str] = None
        accepted = 0
        for candidate in candidates:
            draft_id = save_comment_draft(
                self.conn,
                int(media["media_pk"]),
                candidate.text,
                candidate.text.strip().lower(),
                language=str(self.config.get("comments.language", "ko")),
                quality_ok=candidate.quality_ok,
                quality_reason=candidate.quality_reason,
                similarity_max=candidate.similarity_max,
                status=candidate.status.value,
                generator=candidate.generator,
                generator_version=candidate.generator_version,
            )
            if candidate.quality_ok:
                accepted += 1
                if candidate.status is DraftStatus.SELECTED and selected_text is None:
                    selected_id, selected_text = draft_id, candidate.text
            elif candidate.quality_reason.startswith("duplicate"):
                stats.rejected_duplicate += 1
            else:
                stats.rejected_quality += 1

        stats.generated += accepted
        if accepted == 0:
            stats.none_generated += 1
        return selected_id, selected_text

    # --- Phase 5~6: 실행 -------------------------------------------------
    def execute(self) -> ExecutionSummary:
        executor = create_executor(
            self.config.executor_mode,
            export_dir=self.config.export_dir,
            run_id=self.run_id,
            dry_run=self.config.dry_run,
        )
        rate_limiter = RateLimiter(self.config, self.conn, dry_run=self.config.dry_run)
        controller = ActionController(self.config, self.conn, executor, rate_limiter)

        summary = controller.run()
        self.summary.execution = summary
        self.summary.limits = rate_limiter.usage_summary()
        health = executor.health_check()
        self.summary.export_path = str(health.get("export_path") or "") or None

        with transaction(self.conn):
            set_state(self.conn, "last_run_id", self.run_id)
            set_state(self.conn, "last_run_summary", json.dumps(summary.skip_reasons, ensure_ascii=False))
        return summary

    # --- 전체 실행 --------------------------------------------------------
    def run(self, source_path: Optional[Path | str] = None) -> RunSummary:
        self.ingest_inbox()
        self.discover(source_path)
        self.analyze_and_score()
        self.execute()
        return self.summary


def next_run_id(conn: sqlite3.Connection) -> str:
    """app_state 기반 실행 ID(YYYYmmdd-HHMMSS + 순번)."""
    from datetime import datetime, timezone

    counter = int(get_state(conn, "run_counter", "0") or 0) + 1
    with transaction(conn):
        set_state(conn, "run_counter", str(counter))
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"{stamp}-{counter:04d}"


def format_summary(summary: RunSummary, config: Config) -> str:
    """CLI에 출력할 Run Summary 텍스트."""
    minimum = int(float(config.get("scoring.minimum_target_score", 70)))
    auto = int(float(config.get("scoring.auto_action_score", minimum)))
    like_used, like_max = summary.limits.get("LIKE", (0, 0))
    comment_used, comment_max = summary.limits.get("COMMENT", (0, 0))

    lines = [
        "",
        "=" * 52,
        " DailyReels Targeting Agent",
        "=" * 52,
        f" Run ID      : {summary.run_id}",
        f" Executor    : {summary.executor} (dry_run={summary.dry_run})",
        f" Analyzer    : {summary.analyzer}",
        f" Profile     : {summary.profile_source} (가중치 {summary.weights_source})",
        "",
        " Discovery",
        f"   Found     : {summary.discovery.found}",
        f"   Duplicate : {summary.discovery.duplicate}",
        f"   Filtered  : {summary.discovery.filtered}",
        f"   New       : {summary.discovery.new}",
    ]
    if summary.ingest and summary.ingest.has_input:
        ingest = summary.ingest
        lines += [
            "",
            " Inbox 입력",
            f"   Added     : {ingest.added}",
            f"   Duplicate : {ingest.duplicate}",
            f"   Invalid   : {ingest.invalid}",
            f"   Error     : {ingest.error}",
        ]
    lines += [
        "",
        " Analysis",
        f"   Analyzed       : {summary.analysis.analyzed} (cache {summary.analysis.cached})",
        f"   Score >= {minimum:<5} : {summary.analysis.above_minimum}",
        f"   Score >= {auto:<5} : {summary.analysis.above_auto}",
        f"   Blocked        : {summary.analysis.disqualified}",
        "",
        " Comments",
        f"   Generated : {summary.comments.generated}",
        f"   Rejected  : quality {summary.comments.rejected_quality} / duplicate {summary.comments.rejected_duplicate}",
        "",
        " Action Queue",
        f"   LIKE      : {summary.queue.likes}",
        f"   COMMENT   : {summary.queue.comments}",
        f"   Skipped   : {summary.queue.skipped}",
        "",
        " Daily Limit",
        f"   LIKE      : {like_used} / {like_max}",
        f"   COMMENT   : {comment_used} / {comment_max}",
        "",
        " Result",
        f"   SUCCESS   : {summary.execution.success}",
        f"   SKIPPED   : {summary.execution.skipped}",
        f"   ERROR     : {summary.execution.failed}",
    ]
    if summary.execution.awaiting:
        lines.append(f"   확인 대기 : {summary.execution.awaiting} (처리 후 --confirm)")
    if summary.execution.skip_reasons:
        reasons = ", ".join(f"{k}={v}" for k, v in sorted(summary.execution.skip_reasons.items()))
        lines.append(f"   Skip 사유 : {reasons}")
    if summary.execution.halted:
        lines.append(f"   HALTED    : {summary.execution.halt_reason}")
    if summary.export_path:
        lines.append(f"   수동 실행 목록: {summary.export_path}")
    lines.append("=" * 52)
    return "\n".join(lines)
