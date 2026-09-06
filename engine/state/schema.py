"""Machine State(SQLite) 스키마.

Architecture v3.1 기준:
- source_checkpoints: 소스별(Slack/Drive/WBS/Email 공통 개념) 증분 처리 checkpoint
- folders / files: Drive File/Folder Registry (Processing Registry) — path가 아니라
  drive_file_id를 1급 식별자로 사용한다.
- processing_events: 변경 감지 결과에 대한 감사 로그(이번 MVP에서는 AI 분석 job은 없음)
"""

SCHEMA_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS source_checkpoints (
        project_id TEXT NOT NULL,
        source_type TEXT NOT NULL,
        last_page_token TEXT,
        pending_page_token TEXT,
        last_successful_sync_at TEXT,
        last_run_status TEXT,
        last_error TEXT,
        updated_at TEXT,
        PRIMARY KEY (project_id, source_type)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS folders (
        project_id TEXT NOT NULL,
        folder_id TEXT NOT NULL,
        parent_folder_id TEXT,
        folder_name TEXT,
        in_project_scope INTEGER NOT NULL DEFAULT 0,
        scope_config_version INTEGER NOT NULL DEFAULT 0,
        scope_resolved_at TEXT,
        updated_at TEXT,
        PRIMARY KEY (project_id, folder_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS files (
        internal_file_id INTEGER PRIMARY KEY AUTOINCREMENT,
        project_id TEXT NOT NULL,
        drive_file_id TEXT NOT NULL,
        filename TEXT,
        mime_type TEXT,
        parent_folder_id TEXT,
        path_hint TEXT,
        file_category TEXT,

        size INTEGER,
        modified_time TEXT,
        checksum TEXT,
        drive_version TEXT,

        processing_status TEXT,
        internal_content_version INTEGER NOT NULL DEFAULT 1,

        in_project_scope INTEGER NOT NULL DEFAULT 0,
        is_deleted INTEGER NOT NULL DEFAULT 0,

        first_seen_at TEXT,
        last_seen_at TEXT,
        updated_at TEXT,

        UNIQUE (project_id, drive_file_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS processing_events (
        event_id INTEGER PRIMARY KEY AUTOINCREMENT,
        project_id TEXT NOT NULL,
        drive_file_id TEXT,
        event_type TEXT NOT NULL,
        detected_at TEXT NOT NULL,
        details TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_files_project ON files(project_id)",
    "CREATE INDEX IF NOT EXISTS idx_folders_project ON folders(project_id)",
    "CREATE INDEX IF NOT EXISTS idx_events_project ON processing_events(project_id)",
]
