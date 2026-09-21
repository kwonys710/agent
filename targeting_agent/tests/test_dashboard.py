"""Dashboard 렌더링 테스트(HTTP 서버 기동 없이 HTML만 확인)."""
from __future__ import annotations

from pathlib import Path

from targeting_agent.core.database import enqueue_action, insert_candidate
from targeting_agent.core.models import ActionType
from targeting_agent.dashboard.app import render_page


def test_render_page_with_data(conn, sample_candidate) -> None:
    media_pk = insert_candidate(conn, sample_candidate)
    enqueue_action(
        conn, run_id="r1", media_pk=media_pk, creator_id=1, action_type=ActionType.LIKE,
        target_score=88.0, priority=88, executor="manual", dry_run=True,
    )
    conn.commit()

    html = render_page(conn, Path("data/targeting.db"), "2026-09-21")
    assert "DailyReels Targeting Agent" in html
    assert "office_daily_kim" in html
    assert "NEW" in html


def test_render_page_empty_db(conn) -> None:
    html = render_page(conn, Path("data/targeting.db"), "2026-09-21")
    assert "데이터 없음" in html
