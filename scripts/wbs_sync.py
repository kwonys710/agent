#!/usr/bin/env python3
"""WBS SOURCE CHANGE 실행 CLI.

사용법:
    python scripts/wbs_sync.py --project <id> --bootstrap   # 최초 1회 (수동)
    python scripts/wbs_sync.py --project <id>               # 일상 incremental

주의: 이번 단계에서는 Google Sheets 인증 배선이 없다.
실제 GoogleSheetsClient 를 자동 생성하려 하면 명확한 오류로 중단한다
(OAuth scope 추가/토큰 처리는 이 커밋 범위 밖 — Commit 4+ 에서 배선).
orchestration 로직(run_wbs_bootstrap / run_wbs_sync)은 Fake 클라이언트로 테스트한다.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv

from engine.config.loader import ConfigError, load_project_config
from engine.state.database import get_connection, init_db
from engine.wbs.sync import (
    WBSBootstrapAlreadyDone,
    WBSBootstrapRequired,
    WBSNotConfigured,
    format_report,
    run_wbs_bootstrap,
    run_wbs_sync,
)


class SheetsClientNotConfigured(RuntimeError):
    pass


def build_sheets_client():
    raise SheetsClientNotConfigured(
        "WBS Google Sheets 클라이언트 인증 배선이 아직 없습니다. "
        "이 커밋 범위에서는 실제 Google API 실행을 지원하지 않습니다 "
        "(orchestration 은 Fake 클라이언트 테스트로 검증)."
    )


def run_cli(argv=None, *, client_factory=build_sheets_client, connection=None) -> int:
    load_dotenv()

    parser = argparse.ArgumentParser(description="WBS state sync (SOURCE CHANGE 경로)")
    parser.add_argument("--project", required=True)
    parser.add_argument("--bootstrap", action="store_true", help="최초 1회 state inventory 생성")
    parser.add_argument("--override", action="store_true", help="bootstrap 재실행 허용 (state truncate 하지 않음)")
    args = parser.parse_args(argv)

    try:
        config = load_project_config(args.project)
    except ConfigError as exc:
        print(f"[ERROR] {exc}")
        return 1

    if config.wbs is None:
        print(f"[SKIPPED] 프로젝트 '{args.project}' 에는 wbs 섹션이 없습니다. 대상 아님.")
        return 0

    conn = connection or get_connection()
    init_db(conn)

    try:
        client = client_factory()
    except SheetsClientNotConfigured as exc:
        print(f"[ERROR] {exc}")
        return 3

    try:
        if args.bootstrap:
            report = run_wbs_bootstrap(conn, config, client, override=args.override)
        else:
            report = run_wbs_sync(conn, config, client)
    except WBSNotConfigured as exc:
        print(f"[SKIPPED] {exc}")
        return 0
    except WBSBootstrapAlreadyDone as exc:
        print(f"[SKIPPED] {exc}")
        return 2
    except WBSBootstrapRequired as exc:
        print(f"[ERROR] {exc}")
        return 3
    except Exception as exc:  # noqa: BLE001
        print(f"[ERROR] WBS sync 실패: {exc}")
        print("\nResult: FAILED (checkpoint 는 전진하지 않았습니다.)")
        return 1

    print(format_report(report))
    return 0


def main() -> None:
    sys.exit(run_cli())


if __name__ == "__main__":
    main()
