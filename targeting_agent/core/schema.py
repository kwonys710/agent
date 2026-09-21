"""SQLite 스키마 정의(data/targeting.db).

중복 방지는 애플리케이션 로직뿐 아니라 DB 제약으로도 강제한다.
- creators.username UNIQUE
- candidate_media.media_id UNIQUE  (동일 게시물 재수집 차단)
- interactions(media_pk, action_type, dry_run) UNIQUE (동일 media 중복 Interaction 차단)
- action_queue(media_pk, action_type) UNIQUE (동일 Action 중복 적재 차단)
- comment_drafts(media_pk, normalized_text) UNIQUE (동일 댓글 중복 저장 차단)
- candidate_media.canonical_url UNIQUE (URL 입력 시 동일 게시물 중복 차단)

v6(Phase 15): scheduled_runs — Scheduled Run 이력.

v5(Phase 12B): import_events.note — 입력 시 남긴 메모(후보 테이블은 건드리지 않는다).

v4(Phase 14): action_queue.approved_at — Dashboard에서 운영자가 승인한 시각.
실행 상태(status)와 분리해 두어야 Phase 9의 '확인 대기(APPROVED)' 의미와 충돌하지 않는다.

v3(Phase 13): ai_analysis_cache 추가 — 동일 Candidate+Prompt+Model 재호출 차단.

v2(Phase 12A): candidate_media에 canonical_url / instagram_media_id 추가,
import_events 테이블 추가. 기존 DB는 core/database.py의 마이그레이션으로 보강된다.
"""
from __future__ import annotations

SCHEMA_VERSION = 6

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
        canonical_url TEXT,
        instagram_media_id TEXT,
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
    # URL 입력 경로의 중복 차단. NULL은 제외해야 기존 행과 충돌하지 않는다.
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_candidate_canonical "
    "ON candidate_media(canonical_url) WHERE canonical_url IS NOT NULL",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_candidate_ig_media "
    "ON candidate_media(instagram_media_id) WHERE instagram_media_id IS NOT NULL",
    """
    CREATE TABLE IF NOT EXISTS import_events (
        import_id INTEGER PRIMARY KEY AUTOINCREMENT,
        media_pk INTEGER REFERENCES candidate_media(media_pk) ON DELETE SET NULL,
        source TEXT NOT NULL,
        source_file TEXT,
        source_row INTEGER,
        original_url TEXT,
        canonical_url TEXT,
        result TEXT NOT NULL,
        error_message TEXT,
        note TEXT,
        imported_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_import_result ON import_events(result, imported_at)",
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
        approved_at TEXT,
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
    CREATE TABLE IF NOT EXISTS ai_analysis_cache (
        cache_key TEXT PRIMARY KEY,
        model TEXT NOT NULL,
        prompt_version TEXT NOT NULL,
        media_pk INTEGER REFERENCES candidate_media(media_pk) ON DELETE SET NULL,
        payload TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS scheduled_runs (
        run_id INTEGER PRIMARY KEY AUTOINCREMENT,
        started_at TEXT NOT NULL,
        finished_at TEXT,
        status TEXT NOT NULL,
        candidates_processed INTEGER NOT NULL DEFAULT 0,
        claude_calls INTEGER NOT NULL DEFAULT 0,
        cache_hits INTEGER NOT NULL DEFAULT 0,
        fallbacks INTEGER NOT NULL DEFAULT 0,
        errors INTEGER NOT NULL DEFAULT 0,
        summary_file TEXT
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
