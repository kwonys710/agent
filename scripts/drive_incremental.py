#!/usr/bin/env python3
"""Incremental: 일상 자동 실행(스케줄러가 호출).

절대로 Bootstrap을 대신 실행하지 않는다 — checkpoint가 없으면 에러로 중단한다.

사용법:
    python scripts/drive_incremental.py --project jungsoo
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv

from engine.config.loader import ConfigError, load_project_config
from engine.drive.client import GoogleDriveClient
from engine.drive.incremental import BootstrapRequired, run_incremental
from engine.state.database import get_connection, init_db


def _print_list(label: str, items: list) -> None:
    print(f"\n{label}:")
    if items:
        for item in items:
            print(f"- {item}")
    else:
        print("- 없음")


def main() -> None:
    load_dotenv()

    parser = argparse.ArgumentParser(description="Drive Incremental — 변경분만 감지/반영")
    parser.add_argument("--project", required=True)
    args = parser.parse_args()

    try:
        config = load_project_config(args.project)
    except ConfigError as exc:
        print(f"[ERROR] {exc}")
        sys.exit(1)

    conn = get_connection()
    init_db(conn)
    drive_client = GoogleDriveClient()

    try:
        report = run_incremental(conn, config, drive_client)
    except BootstrapRequired as exc:
        print(f"[ERROR] {exc}")
        sys.exit(3)
    except Exception as exc:
        print(f"[ERROR] Incremental 실행 실패: {exc}")
        print("\nResult: FAILED (checkpoint는 이동하지 않았습니다. 다음 실행에서 동일 구간을 재처리합니다.)")
        sys.exit(1)

    print("[Drive Incremental Scan]\n")
    print(f"Project: {config.project_name}")

    _print_list("New", report["new"])
    _print_list("Modified", report["modified"])
    _print_list("Renamed/Moved", report["renamed"] + report["moved"])
    _print_list("Deleted", report["deleted"])

    print(f"\nOut of Scope:\n- {report['out_of_scope']}건")
    print("\nUnchanged files scanned:\n- 0")

    print(f"\nDrive Changes events received: {report['changes_received']}")
    print(f"Project scope matched: {report['scope_matched']}")
    print(f"Metadata-only (무시): {report['metadata_only']}")
    print(f"Content files downloaded: {report['content_downloads']}")
    print(f"Claude API calls: {report['claude_api_calls']}")

    print(f"\nResult: {report['result']}")


if __name__ == "__main__":
    main()
