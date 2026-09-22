"""DailyReels Targeting Agent 진입점.

실행 예:
    python -m targeting_agent.main                      # config.yaml 기준 실행(기본 Dry Run)
    python -m targeting_agent.main --import data.csv    # 후보 파일 지정
    python -m targeting_agent.main --no-dry-run         # 실제 실행(ManualExecutor는 목록 생성)
    python -m targeting_agent.main --stats              # 현재 DB 현황만 출력
    python -m targeting_agent.main --dashboard          # 로컬 Dashboard 실행
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional, Sequence

if __package__ in (None, ""):  # pragma: no cover - `python targeting_agent/main.py` 직접 실행 대응
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    __package__ = "targeting_agent"

from .core.config import Config, load_config
from .core.database import (
    get_connection,
    utc_now,
    get_stats,
    init_db,
    reset_skipped_for_rescore,
    today_str,
)
from .actions.confirm import (
    confirm_actions,
    format_awaiting,
    list_awaiting,
    parse_ids,
    skip_actions,
)
from .analysis.profile_analyzer import load_profile
from .core.exceptions import TargetingError
from .core.logger import get_logger, setup_logging
from .core.models import FeedbackType
from .discovery.ingest import CandidateIngestor, ImportSummary
from .discovery.ingest import format_summary as format_import_summary
from .learning.feedback import record_for_target, resolve_target
from .learning.comment_stats import collect_comment_preference
from .learning.metrics import collect_health_metrics, collect_quality_metrics, recommend_thresholds
from .learning.profiles import SOURCE_LEARNING, get_active_profile
from .learning.profiles import create_version as create_profile_version
from .learning.profiles import rollback as rollback_profile
from .learning.topics import STATUS_OK, format_plan, plan_learning
from .learning.profile_optimizer import (
    apply_learning,
    format_suggestions,
    load_weights_override,
    reset_learning,
    should_update,
    suggest_profile,
    suggest_weights,
)
from .pipeline import TargetingPipeline, format_summary, next_run_id

logger = get_logger("main")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="targeting_agent", description="DailyReels Targeting Agent"
    )
    parser.add_argument("--config", type=Path, default=None, help="config.yaml 경로")
    parser.add_argument(
        "--import", dest="import_path", type=Path, default=None, help="후보 CSV/JSON/TXT 경로"
    )
    parser.add_argument("--limit", type=int, default=None, help="이번 실행 최대 Action 수 override")
    parser.add_argument("--executor", default=None, help="executor.mode override (manual 등)")
    dry = parser.add_mutually_exclusive_group()
    dry.add_argument("--dry-run", dest="dry_run", action="store_true", help="Dry Run 강제(기본)")
    dry.add_argument(
        "--no-dry-run", dest="dry_run", action="store_false", help="실제 실행(주의)"
    )
    parser.set_defaults(dry_run=None)
    parser.add_argument(
        "--add",
        action="append",
        metavar="URL",
        default=None,
        help="Instagram 게시물 URL을 후보로 등록(여러 번 지정 가능)",
    )
    parser.add_argument("--username", default=None, help="--add 시 Creator username(선택)")
    parser.add_argument("--caption", default="", help="--add 시 캡션 직접 입력(선택)")
    parser.add_argument(
        "--import-only",
        action="store_true",
        help="후보 입력(inbox 포함)만 수행하고 분석/실행은 하지 않는다",
    )
    parser.add_argument(
        "--scheduled",
        action="store_true",
        help="Scheduled Run(배치): inbox 처리 → 신규 후보 분석 → Daily Summary 생성",
    )
    parser.add_argument(
        "--discover",
        action="store_true",
        help="Browser Discovery: Instagram 검색으로 Reel 후보 수집 → 저장 → 분석(읽기 전용)",
    )
    parser.add_argument(
        "--discover-only",
        action="store_true",
        help="Browser Discovery를 수집·저장까지만 수행(분석/Claude 호출 없음)",
    )
    parser.add_argument(
        "--query",
        action="append",
        default=None,
        metavar="검색어",
        help="Browser Discovery 검색어 override(여러 번 지정 가능)",
    )
    parser.add_argument("--stats", action="store_true", help="DB 현황만 출력하고 종료")
    parser.add_argument(
        "--list-pending", action="store_true", help="운영자 확인 대기 Action 목록 출력"
    )
    parser.add_argument(
        "--confirm", metavar="ID|all", default=None, help="직접 처리한 Action을 기록"
    )
    parser.add_argument(
        "--skip", metavar="ID|all", default=None, help="처리하지 않은 Action을 취소"
    )
    parser.add_argument("--dashboard", action="store_true", help="로컬 Dashboard 실행")
    parser.add_argument("--seed", type=int, default=None, help="댓글 생성 난수 seed(테스트용)")
    parser.add_argument(
        "--feedback",
        metavar="TYPE",
        default=None,
        help="Feedback 기록: " + " | ".join(t.value for t in FeedbackType),
    )
    parser.add_argument(
        "--target", metavar="ID", default=None, help="Feedback 대상: Action ID | @username | media_id"
    )
    parser.add_argument("--note", default="", help="Feedback 메모")
    parser.add_argument(
        "--learn",
        action="store_true",
        help="Feedback 기반 조정 제안 출력(미리보기 전용 — Profile을 바꾸지 않는다)",
    )
    parser.add_argument("--learn-apply", action="store_true", help="조정 제안을 실제로 반영")
    parser.add_argument("--learn-reset", action="store_true", help="학습 반영 내용 초기화")
    parser.add_argument(
        "--learning-rollback", action="store_true", help="직전 Target Profile 버전으로 되돌린다"
    )
    parser.add_argument(
        "--rescore",
        action="store_true",
        help="점수 미달로 제외된 후보를 다시 채점 대상으로 되돌린다(학습 반영 후 사용)",
    )
    return parser


def apply_overrides(config: Config, args: argparse.Namespace) -> Config:
    """CLI 인자를 config에 반영한 새 Config를 만든다(원본 불변)."""
    raw = {k: (dict(v) if isinstance(v, dict) else v) for k, v in config.raw.items()}
    if args.dry_run is not None:
        raw.setdefault("actions", {}).setdefault("execution", {})
        raw["actions"] = {**raw["actions"], "execution": {**raw["actions"]["execution"], "dry_run": bool(args.dry_run)}}
    if args.executor:
        raw["executor"] = {**raw.get("executor", {}), "mode": args.executor}
    if args.limit is not None:
        execution = {**raw.get("actions", {}).get("execution", {}), "max_actions_per_run": int(args.limit)}
        raw["actions"] = {**raw.get("actions", {}), "execution": execution}
    if getattr(args, "discover", False) or getattr(args, "discover_only", False):
        # 운영자가 --discover 로 명시적으로 실행할 때만 켠다(Scheduler 자동 연결 없음).
        raw["browser_discovery"] = {**raw.get("browser_discovery", {}), "enabled": True}
    return Config(raw=raw, path=config.path, base_dir=config.base_dir)


def print_stats(config: Config) -> int:
    conn = get_connection(config.db_path)
    try:
        init_db(conn)
        date = today_str(int(config.get("actions.daily_limits.timezone_offset_hours", 9)))
        stats = get_stats(conn, date)
        rows = conn.execute(
            "SELECT status, COUNT(*) AS n FROM candidate_media GROUP BY status"
        ).fetchall()
        print(f"[DB] {config.db_path}")
        print(f"[오늘 통계 {date}] " + (", ".join(f"{k}={v}" for k, v in sorted(stats.items())) or "없음"))
        print("[후보 상태] " + (", ".join(f"{r['status']}={r['n']}" for r in rows) or "없음"))
    finally:
        conn.close()
    return 0


def run_confirmation(config: Config, args: argparse.Namespace) -> int:
    """--list-pending / --confirm / --skip 처리."""
    conn = get_connection(config.db_path)
    try:
        init_db(conn)
        if args.list_pending:
            print(format_awaiting(list_awaiting(conn)))
            return 0

        raw = args.confirm if args.confirm is not None else args.skip
        try:
            select_all, ids = parse_ids(str(raw))
        except ValueError as exc:
            print(f"[오류] {exc}", file=sys.stderr)
            return 2
        if not select_all and not ids:
            print("[오류] Action ID 또는 all 을 지정하세요.", file=sys.stderr)
            return 2

        if args.confirm is not None:
            result = confirm_actions(conn, config, select_all=select_all, action_ids=ids)
            print(f"확인 완료: {result.confirmed}건")
            for message in result.over_limit:
                print(f"[경고] {message}")
        else:
            result = skip_actions(conn, select_all=select_all, action_ids=ids)
            print(f"취소 완료: {result.skipped}건")

        if result.not_found:
            missing = ", ".join(str(i) for i in result.not_found)
            print(f"[알림] 확인 대기 목록에 없는 Action ID: {missing}")
        return 0
    finally:
        conn.close()


def run_add(config: Config, args: argparse.Namespace) -> int:
    """--add URL [--username X] [--caption ...] 처리."""
    conn = get_connection(config.db_path)
    try:
        init_db(conn)
        ingestor = CandidateIngestor(conn)
        urls = [str(url) for url in args.add]
        if args.caption and len(urls) == 1:
            # 캡션은 단건 입력일 때만 반영한다(여러 URL에 같은 캡션을 붙이지 않는다).
            summary = ImportSummary()
            ingestor.add_url(
                urls[0],
                username=args.username,
                caption=args.caption,
                note=args.note,
                summary=summary,
            )
        else:
            summary = ingestor.add_urls(urls, username=args.username, note=args.note)
        print(format_import_summary(summary))
        return 0 if summary.added or summary.duplicate else 1
    finally:
        conn.close()


def run_import_only(config: Config, args: argparse.Namespace) -> int:
    """inbox CSV만 처리하고 분석/실행은 하지 않는다."""
    conn = get_connection(config.db_path)
    try:
        init_db(conn)
        inbox = config._resolve_path(config.get("discovery.inbox.path", "data/inbox"))
        summary = CandidateIngestor(conn).process_inbox(
            inbox,
            processed_dir=config._resolve_path(
                config.get("discovery.inbox.processed_dir", "data/inbox/processed")
            ),
            failed_dir=config._resolve_path(
                config.get("discovery.inbox.failed_dir", "data/inbox/failed")
            ),
        )
        if not summary.has_input:
            print(f"처리할 입력이 없습니다: {inbox}")
            return 0
        print(format_import_summary(summary))
        return 0
    finally:
        conn.close()


def run_feedback(config: Config, args: argparse.Namespace) -> int:
    """--feedback TYPE --target ID 처리."""
    try:
        feedback_type = FeedbackType(str(args.feedback).strip().upper())
    except ValueError:
        valid = ", ".join(t.value for t in FeedbackType)
        print(f"[오류] 알 수 없는 Feedback 타입입니다. 사용 가능: {valid}", file=sys.stderr)
        return 2

    conn = get_connection(config.db_path)
    try:
        init_db(conn)
        try:
            target = resolve_target(conn, str(args.target or ""))
        except ValueError as exc:
            print(f"[오류] {exc}", file=sys.stderr)
            return 2
        record_for_target(conn, feedback_type, target, note=args.note)
        conn.commit()
        print(f"Feedback 기록: {feedback_type.value} → {target.label}")
        return 0
    finally:
        conn.close()


def _record_learning_run(conn, started, plan, previous_version, new_version, applied: bool) -> None:
    """학습 이력을 남긴다(dry-run 포함)."""
    import json as _json

    conn.execute(
        "INSERT INTO learning_runs (started_at, finished_at, feedback_count, previous_profile, "
        "new_profile, changed_topics, status, dry_run) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            started,
            utc_now(),
            plan.feedback_count,
            previous_version,
            new_version,
            _json.dumps([c.as_line() for c in plan.changes], ensure_ascii=False),
            plan.status,
            int(not applied),
        ),
    )
    conn.commit()


def format_operations_report(conn, config: Config) -> str:
    """품질·운영 지표와 임계값 추천(설정은 바꾸지 않는다)."""
    quality = collect_quality_metrics(conn)
    health = collect_health_metrics(conn, config)
    preference = collect_comment_preference(conn)

    lines = ["운영 지표", "", "  [검토 품질]"]
    lines += [f"    {k}: {v}" for k, v in quality.as_rows().items()]
    lines += ["", "  [댓글 선호]"]
    lines += [f"    {k}: {v}" for k, v in preference.as_rows().items()]
    lines += ["", "  [운영 상태]"]
    lines += [f"    {k}: {v}" for k, v in health.as_rows().items()]
    for warning in health.warnings:
        lines.append(f"    [경고] {warning}")
    lines += ["", "  [임계값 추천]"]
    lines += [f"    - {text}" for text in recommend_thresholds(config, quality)]
    return "\n".join(lines)


def run_learning_rollback(config: Config) -> int:
    """직전 Profile 버전으로 되돌린다."""
    conn = get_connection(config.db_path)
    try:
        init_db(conn)
        ok, message = rollback_profile(conn)
        print(message)
        return 0 if ok else 1
    finally:
        conn.close()


def run_learning(config: Config, args: argparse.Namespace) -> int:
    """--learn / --learn-apply / --learn-reset 처리."""
    conn = get_connection(config.db_path)
    try:
        init_db(conn)
        if args.learn_reset:
            reset_learning(conn)
            print("학습 반영 내용을 초기화했습니다(config.yaml / profile 파일 기준으로 복귀).")
            return 0

        profile = load_profile(config.profile_path, conn)
        min_samples = int(config.get("learning.min_feedback_samples", 10))
        profile_suggestion = suggest_profile(
            conn,
            profile,
            min_samples=min_samples,
            min_occurrences=int(config.get("learning.min_keyword_occurrences", 3)),
            min_score=float(config.get("learning.min_keyword_score", 2.0)),
            min_media=int(config.get("learning.min_keyword_media", 2)),
        )
        current_weights = load_weights_override(conn) or {
            k: float(v) for k, v in config.section("scoring.weights").items()
        }
        weight_suggestion = suggest_weights(conn, current_weights, min_samples=min_samples)

        # Phase 17: Topic Weight 학습(결정론적, Claude 호출 없음)
        active = get_active_profile(conn)
        plan = plan_learning(conn, config, active.topic_weights)
        started = utc_now()

        applied = None
        if args.learn_apply:
            interval = int(config.get("learning.profile_update_interval_days", 0))
            if not should_update(conn, interval):
                print(
                    f"[알림] 마지막 반영 이후 {interval}일이 지나지 않아 건너뜁니다 "
                    "(learning.profile_update_interval_days)."
                )
                return 0
            applied = apply_learning(conn, profile, profile_suggestion, weight_suggestion)

        print(format_suggestions(profile_suggestion, weight_suggestion, applied=applied))
        print()
        print(format_plan(plan, active.version))

        new_version = None
        if args.learn_apply and plan.status == STATUS_OK and plan.has_change:
            created = create_profile_version(
                conn,
                plan.new_weights,
                source=SOURCE_LEARNING,
                reason=f"Feedback {plan.feedback_count}건 기반 자동 조정",
                previous_version=active.version,
            )
            new_version = created.version
            print(f"\n  → Profile {active.version} → {created.version} 적용 완료")
        elif args.learn_apply:
            print("\n  → 적용할 Topic Weight 변경이 없습니다.")
        else:
            print("\n  (적용하려면 --learn-apply, 되돌리려면 --learning-rollback)")

        _record_learning_run(conn, started, plan, active.version, new_version, args.learn_apply)
        print()
        print(format_operations_report(conn, config))
        return 0
    finally:
        conn.close()


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    try:
        config = load_config(args.config)
    except TargetingError as exc:
        print(f"[오류] {exc}", file=sys.stderr)
        return 2

    config = apply_overrides(config, args)
    setup_logging(
        level=str(config.get("logging.level", "INFO")),
        log_dir=config.log_dir,
        keep_days=int(config.get("logging.keep_days", 30)),
    )

    if args.dashboard:
        if not bool(config.get("dashboard.enabled", True)):
            print("[오류] dashboard.enabled 가 false 입니다.", file=sys.stderr)
            return 2
        from .dashboard.app import serve

        try:
            serve(config)
        except TargetingError as exc:
            print(f"[오류] {exc}", file=sys.stderr)
            return 2
        return 0

    if args.add:
        return run_add(config, args)

    if args.import_only:
        return run_import_only(config, args)

    if args.discover or args.discover_only:
        from .discovery.browser_runner import format_result, run_browser_discovery

        result = run_browser_discovery(
            config, queries=args.query, process=not args.discover_only
        )
        print(format_result(result))
        return result.exit_code

    if args.scheduled:
        from .scheduler.runner import format_result, run_scheduled

        result = run_scheduled(config)
        print(format_result(result))
        return result.exit_code

    if args.rescore:
        conn = get_connection(config.db_path)
        try:
            init_db(conn)
            count = reset_skipped_for_rescore(conn)
            print(f"재채점 대상으로 되돌린 후보: {count}건 (다음 실행에서 다시 평가됩니다)")
            return 0
        finally:
            conn.close()

    if args.feedback is not None:
        return run_feedback(config, args)

    if args.learning_rollback:
        return run_learning_rollback(config)

    if args.learn or args.learn_apply or args.learn_reset:
        return run_learning(config, args)

    if args.list_pending or args.confirm is not None or args.skip is not None:
        return run_confirmation(config, args)

    if args.stats:
        return print_stats(config)

    conn = get_connection(config.db_path)
    try:
        init_db(conn)
        run_id = next_run_id(conn)
        logger.info(
            "실행 시작 run_id=%s executor=%s dry_run=%s",
            run_id,
            config.executor_mode,
            config.dry_run,
        )
        pipeline = TargetingPipeline(config, conn, run_id=run_id, seed=args.seed)
        summary = pipeline.run(args.import_path)
        print(format_summary(summary, config))
        if summary.execution.halted:
            return 1
        return 0
    except TargetingError as exc:
        logger.error("실행 실패: %s", exc)
        print(f"[오류] {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:  # pragma: no cover - 사용자 중단
        logger.warning("사용자에 의해 중단되었습니다. 다음 실행 시 이어서 처리합니다.")
        return 130
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
