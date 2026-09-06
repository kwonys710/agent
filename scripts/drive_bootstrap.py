#!/usr/bin/env python3
"""Bootstrap: 프로젝트 최초 등록 시 1회만 실행한다.

사용법:
    python scripts/drive_bootstrap.py --project jungsoo
    python scripts/drive_bootstrap.py --project jungsoo --override   # 강제 재실행
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv

from engine.config.loader import ConfigError, load_project_config
from engine.drive.bootstrap import BootstrapAlreadyDone, run_bootstrap
from engine.drive.client import GoogleDriveClient
from engine.state.database import get_connection, init_db


def main() -> None:
    load_dotenv()

    parser = argparse.ArgumentParser(description="Drive Bootstrap — 프로젝트 File/Folder Inventory 최초 생성")
    parser.add_argument("--project", required=True, help="projects/<project_id>/config.yaml의 project_id")
    parser.add_argument("--override", action="store_true", help="기존 checkpoint가 있어도 강제로 재실행")
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
        result = run_bootstrap(conn, config, drive_client, override=args.override)
    except BootstrapAlreadyDone as exc:
        print(f"[SKIPPED] {exc}")
        sys.exit(2)

    print("[Drive Bootstrap]\n")
    print(f"Project: {config.project_name} ({config.project_id})")
    print(f"Folders visited: {result['folders_visited']}")
    print(f"Files registered: {result['files_registered']}")
    print(f"Start page token: {result['start_page_token']}")
    print("Content files downloaded: 0")
    print("Claude API calls: 0")
    print("\nResult: SUCCESS")


if __name__ == "__main__":
    main()
