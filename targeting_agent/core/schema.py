"""SQLite 스키마 정의(data/targeting.db).

중복 방지는 애플리케이션 로직뿐 아니라 DB 제약으로도 강제한다.
- creators.username UNIQUE
- candidate_media.media_id UNIQUE  (동일 게시물 재수집 차단)
- interactions(media_pk, action_type, dry_run) UNIQUE (동일 media 중복 Interaction 차단)
- action_queue(media_pk, action_type) UNIQUE (동일 Action 중복 적재 차단)
- comment_drafts(media_pk, normalized_text) UNIQUE (동일 댓글 중복 저장 차단)
"""
from __future__ import annotations

SCHEMA_VERSION = 1

SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS creators (
        creator_id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT NOT NULL UNIQUE,
        full_name TEXT,
        followers INTEGER NOT NULL DEFAULT 0,
        media_count INTEGER NOT NULL DEFAULT 0,
        is_private INTEGER NOT NULL DEFAULT 0,
        language TEXT,
        profile_summary TEXT,
        status TEXT NOT NULL DEFAULT 'ACTIVE',
        first_seen_at TEXT NOT NULL,
        last_interacted_at TEXT,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS candidate_media (
        media_pk INTEGER PRIMARY KEY AUTOINCREMENT,
        media_id TEXT NOT NULL UNIQUE,
        creator_id INTEGER NOT NULL REFERENCES creators(creator_id) ON DELETE CASCADE,
        permalink TEXT NOT NULL,
        media_type TEXT NOT NULL DEFAULT 'REEL',
        caption TEXT,
        hashtags TEXT,
        like_count INTEGER NOT NULL DEFAULT 0,
        comment_count INTEGER NOT NULL DEFAULT 0,
        posted_at TEXT,
        language TEXT,
        is_ad INTEGER NOT NULL DEFAULT 0,
        source TEXT NOT NULL DEFAULT 'import',
        status TEXT NOT NULL DEFAULT 'NEW',
        status_reason TEXT,
        target_score REAL,
        discovered_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_candidate_status ON candidate_media(status)",
    "CREATE INDEX IF NOT EXISTS idx_candidate_creator ON candidate_media(creator_id)",
    """
    CREATE TABLE IF NOT EXISTS media_analysis (
        analysis_id INTEGER PRIMARY KEY AUTOINCREMENT,
        media_pk INTEGER NOT NULL REFERENCES candidate_media(media_pk) ON DELETE CASCADE,
        analyzer TEXT NOT NULL,
        analyzer_version TEXT NOT NULL,
        prompt_version TEXT NOT NULL,
        payload TEXT NOT NULL,
        similarity REAL,
        target_score REAL,
        score_breakdown TEXT,
        analyzed_at TEXT NOT NULL,
        UNIQUE (media_pk, analyzer, analyzer_version, prompt_version)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS comment_drafts (
        draft_id INTEGER PRIMARY KEY AUTOINCREMENT,
        media_pk INTEGER NOT NULL REFERENCES candidate_media(media_pk) ON DELETE CASCADE,
        text TEXT NOT NULL,
        normalized_text TEXT NOT NULL,
        language TEXT,
        length INTEGER NOT NULL,
        quality_ok INTEGER NOT NULL DEFAULT 1,
        quality_reason TEXT,
        similarity_max REAL NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'CANDIDATE',
        generator TEXT NOT NULL,
        generator_version TEXT NOT NULL,
        created_at TEXT NOT NULL,
        UNIQUE (media_pk, normalized_text)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS action_queue (
        action_id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id TEXT NOT NULL,
        media_pk INTEGER NOT NULL REFERENCES candidate_media(media_pk) ON DELETE CASCADE,
        creator_id INTEGER NOT NULL REFERENCES creators(creator_id) ON DELETE CASCADE,
        draft_id INTEGER REFERENCES comment_drafts(draft_id) ON DELETE SET NULL,
        action_type TEXT NOT NULL CHECK (action_type IN ('LIKE', 'COMMENT')),
        comment_text TEXT,
        target_score REAL NOT NULL DEFAULT 0,
        priority INTEGER NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'PENDING'
            CHECK (status IN ('PENDING','APPROVED','RUNNING','SUCCESS','FAILED','SKIPPED','CANCELLED')),
        executor TEXT NOT NULL DEFAULT 'manual',
        dry_run INTEGER NOT NULL DEFAULT 1,
        scheduled_at TEXT,
        started_at TEXT,
        finished_at TEXT,
        result TEXT,
        error TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE (media_pk, action_type)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_action_status ON action_queue(status)",
    """
    CREATE TABLE IF NOT EXISTS interactions (
        interaction_id INTEGER PRIMARY KEY AUTOINCREMENT,
        media_pk INTEGER NOT NULL REFERENCES candidate_media(media_pk) ON DELETE CASCADE,
        creator_id INTEGER NOT NULL REFERENCES creators(creator_id) ON DELETE CASCADE,
        action_id INTEGER REFERENCES action_queue(action_id) ON DELETE SET NULL,
        action_type TEXT NOT NULL CHECK (action_type IN ('LIKE', 'COMMENT')),
        comment_text TEXT,
        executor TEXT NOT NULL,
        dry_run INTEGER NOT NULL DEFAULT 1,
        success INTEGER NOT NULL DEFAULT 0,
        error TEXT,
        executed_at TEXT NOT NULL,
        UNIQUE (media_pk, action_type, dry_run)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_interactions_creator ON interactions(creator_id, executed_at)",
    """
    CREATE TABLE IF NOT EXISTS feedback (
        feedback_id INTEGER PRIMARY KEY AUTOINCREMENT,
        media_pk INTEGER REFERENCES candidate_media(media_pk) ON DELETE SET NULL,
        creator_id INTEGER REFERENCES creators(creator_id) ON DELETE SET NULL,
        action_id INTEGER REFERENCES action_queue(action_id) ON DELETE SET NULL,
        feedback_type TEXT NOT NULL,
        value REAL NOT NULL DEFAULT 1,
        note TEXT,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS daily_stats (
        stat_date TEXT NOT NULL,
        metric TEXT NOT NULL,
        value INTEGER NOT NULL DEFAULT 0,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (stat_date, metric)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS app_state (
        key TEXT PRIMARY KEY,
        value TEXT,
        updated_at TEXT NOT NULL
    )
    """,
)
