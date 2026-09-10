"""Machine State(SQLite) 스키마.

Architecture v3.1 기준:
- source_checkpoints: 소스별(Slack/Drive/WBS/Email 공통 개념) 증분 처리 checkpoint.
  WBS는 향후 source_type='wbs'로 이 테이블을 그대로 재사용한다(스키마 변경 없음).
- folders / files: Drive File/Folder Registry (Processing Registry) — path가 아니라
  drive_file_id를 1급 식별자로 사용한다.
- processing_events: Drive 변경 감지 결과에 대한 감사 로그. Drive 전용이며 이번
  WBS 작업에서 수정하지 않는다(WBS 전용 event는 후속 commit에서 별도 검토).

WBS Daily To-Do(Commit 1 scaffold):
- wbs_task_state: WBS 행별 정규화 상태 스냅샷. internal_task_id(시스템 내부 안정
  식별자)가 PK이고, source_task_id(WBS 원본 ID 컬럼)와 분리되어 있다 —
  원본 task_id가 중복돼도 PK 충돌 없이 두 행을 저장할 수 있다.
- daily_todo_run / daily_todo_item: 생성된 Daily To-Do 실행/항목 기록(추적성·재실행
  idempotency·FOLLOW_UP 추적용). 이번 commit은 테이블 생성까지만 하고 쓰기 로직은 없다.
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
    # ------------------------------------------------------------------
    # WBS Daily To-Do — scaffold only (Commit 1). 쓰기 로직 없음.
    # ------------------------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS wbs_task_state (
        internal_task_id TEXT PRIMARY KEY,

        project_id TEXT NOT NULL,
        spreadsheet_id TEXT NOT NULL,
        sheet_name TEXT NOT NULL,

        source_task_id TEXT,
        source_identity_key TEXT,
        row_key TEXT NOT NULL,

        task_name TEXT,
        owner TEXT,
        start_date TEXT,
        end_date TEXT,
        progress REAL,
        raw_status TEXT,
        normalized_status TEXT,
        priority TEXT,
        dependency TEXT,
        notes TEXT,
        parse_errors TEXT,

        row_hash TEXT NOT NULL,

        first_seen_at TEXT,
        last_seen_at TEXT,
        last_changed_at TEXT,

        is_active INTEGER NOT NULL DEFAULT 1
    )
    """,
    # source_task_id에는 UNIQUE를 걸지 않는다 — WBS 원본 ID 중복을 허용해야 한다.
    "CREATE INDEX IF NOT EXISTS idx_wbs_task_state_sheet "
    "ON wbs_task_state(project_id, spreadsheet_id, sheet_name)",
    "CREATE INDEX IF NOT EXISTS idx_wbs_task_state_source_task "
    "ON wbs_task_state(project_id, spreadsheet_id, sheet_name, source_task_id)",
    "CREATE INDEX IF NOT EXISTS idx_wbs_task_state_row_key "
    "ON wbs_task_state(project_id, spreadsheet_id, sheet_name, row_key)",
    """
    CREATE TABLE IF NOT EXISTS daily_todo_run (
        run_id INTEGER PRIMARY KEY AUTOINCREMENT,
        project_id TEXT NOT NULL,
        todo_date TEXT NOT NULL,
        status TEXT NOT NULL,
        source_synced INTEGER,
        candidate_count INTEGER,
        claude_used INTEGER NOT NULL DEFAULT 0,
        claude_task_count INTEGER NOT NULL DEFAULT 0,
        guardrail_hit TEXT,
        created_at TEXT,
        -- TODO(후속): 프로젝트당 WBS 소스가 여러 개가 되면 이 UNIQUE는
        -- (project_id, spreadsheet_id, sheet_name, todo_date)로 확장해야 한다.
        UNIQUE (project_id, todo_date)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS daily_todo_item (
        item_id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id INTEGER NOT NULL,
        project_id TEXT NOT NULL,
        todo_date TEXT NOT NULL,

        internal_task_id TEXT NOT NULL,
        source_task_id TEXT,
        row_key TEXT,

        category TEXT NOT NULL,
        also_categories TEXT,
        title TEXT,
        owner TEXT,
        due_date TEXT,
        status TEXT,
        priority TEXT,
        reason TEXT,
        needs_confirmation INTEGER NOT NULL DEFAULT 0,
        resolved INTEGER NOT NULL DEFAULT 0,
        source_ref TEXT,
        created_at TEXT,
        -- 재실행 idempotency는 internal_task_id 기준. source_task_id 기준 UNIQUE 금지.
        UNIQUE (project_id, todo_date, internal_task_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_daily_todo_item_run ON daily_todo_item(run_id)",
]
