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
from .core.database import get_connection, get_stats, init_db, today_str
from .core.exceptions import TargetingError
from .core.logger import get_logger, setup_logging
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
    parser.add_argument("--stats", action="store_true", help="DB 현황만 출력하고 종료")
    parser.add_argument("--dashboard", action="store_true", help="로컬 Dashboard 실행")
    parser.add_argument("--seed", type=int, default=None, help="댓글 생성 난수 seed(테스트용)")
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

        serve(config)
        return 0

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
