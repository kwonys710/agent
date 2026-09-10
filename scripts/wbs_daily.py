#!/usr/bin/env python3
"""Daily To-Do 생성 CLI.

사용법:
    python scripts/wbs_daily.py --project <id>
    python scripts/wbs_daily.py --project <id> --date 2026-09-10   # 재현/테스트용 명시 날짜

주의: Google Sheets 인증 배선은 아직 없다 (Commit 3/5 범위 밖).
실제 GoogleSheetsClient 자동 생성 시 명확히 실패한다. orchestration 은 Fake 로 테스트한다.
Claude / Slack 전송은 이번 단계에서 구현하지 않는다.
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv

from engine.config.loader import ConfigError, load_project_config
from engine.state.database import get_connection, init_db
from engine.todo.daily import DailyPrerequisiteError, format_report, run_daily_todo
from engine.wbs.sync import WBSBootstrapRequired, WBSNotConfigured
from scripts.wbs_sync import SheetsClientNotConfigured, build_sheets_client

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_PREREQUISITE = 3
EXIT_NEEDS_REVIEW = 4


def run_cli(argv=None, *, client_factory=build_sheets_client, connection=None) -> int:
    load_dotenv()

    parser = argparse.ArgumentParser(description="WBS Daily To-Do 생성 (deterministic, Claude/Slack 없음)")
    parser.add_argument("--project", required=True)
    parser.add_argument("--date", help="YYYY-MM-DD (재현/테스트용 명시 날짜)")
    args = parser.parse_args(argv)

    try:
        config = load_project_config(args.project)
    except ConfigError as exc:
        print(f"[ERROR] {exc}")
        return EXIT_ERROR

    if config.wbs is None:
        print(f"[SKIPPED] 프로젝트 '{args.project}' 에는 wbs 섹션이 없습니다. 대상 아님.")
        return EXIT_OK

    today = None
    if args.date:
        try:
            today = dt.date.fromisoformat(args.date)
        except ValueError:
            print(f"[ERROR] --date 형식이 잘못되었습니다: {args.date}")
            return EXIT_ERROR

    conn = connection or get_connection()
    init_db(conn)

    try:
        client = client_factory()
    except SheetsClientNotConfigured as exc:
        print(f"[ERROR] {exc}")
        return EXIT_PREREQUISITE

    try:
        result = run_daily_todo(conn, config, client, today=today)
    except WBSNotConfigured as exc:
        print(f"[SKIPPED] {exc}")
        return EXIT_OK
    except (WBSBootstrapRequired, DailyPrerequisiteError) as exc:
        print(f"[ERROR] {exc}")
        return EXIT_PREREQUISITE
    except Exception as exc:  # noqa: BLE001
        print(f"[ERROR] Daily To-Do 생성 실패: {exc}")
        print("\nResult: FAILED (WBS checkpoint 는 되돌리지 않았습니다.)")
        return EXIT_ERROR

    print(format_report(result))

    return EXIT_NEEDS_REVIEW if result.output["run"]["status"] == "needs_review" else EXIT_OK


def main() -> None:
    sys.exit(run_cli())


if __name__ == "__main__":
    main()
