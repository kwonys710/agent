"""간단한 로컬 Dashboard (Phase 7).

외부 의존성(Streamlit 등) 없이 표준 라이브러리 http.server로 SQLite 현황을 보여준다.
실행: python -m targeting_agent.main --dashboard
기본 주소: http://127.0.0.1:8501  (config.dashboard)
"""
from __future__ import annotations

import html
import sqlite3
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Sequence

from ..core.config import Config, load_config
from ..core.database import get_connection, today_str
from ..core.logger import get_logger

logger = get_logger("dashboard")

PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="ko"><head><meta charset="utf-8">
<title>DailyReels Targeting Agent</title>
<style>
 body {{ font-family: system-ui, "Malgun Gothic", sans-serif; margin: 24px; color: #222; }}
 h1 {{ font-size: 20px; }} h2 {{ font-size: 16px; margin-top: 28px; }}
 table {{ border-collapse: collapse; width: 100%; font-size: 13px; }}
 th, td {{ border: 1px solid #ddd; padding: 6px 8px; text-align: left; }}
 th {{ background: #f5f5f5; }}
 .meta {{ color: #666; font-size: 12px; }}
</style></head>
<body>
<h1>DailyReels Targeting Agent</h1>
<p class="meta">DB: {db} · 기준일: {date}</p>
{sections}
</body></html>
"""


def _table(title: str, headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> str:
    if not rows:
        return f"<h2>{html.escape(title)}</h2><p class='meta'>데이터 없음</p>"
    head = "".join(f"<th>{html.escape(str(h))}</th>" for h in headers)
    body = "".join(
        "<tr>" + "".join(f"<td>{html.escape(str(cell))}</td>" for cell in row) + "</tr>"
        for row in rows
    )
    return f"<h2>{html.escape(title)}</h2><table><tr>{head}</tr>{body}</table>"


def render_page(conn: sqlite3.Connection, db_path: Path, date_str: str) -> str:
    """현재 DB 상태를 HTML로 렌더링한다(조회는 항상 LIMIT을 건다)."""
    sections = []

    rows = conn.execute(
        "SELECT status, COUNT(*) FROM candidate_media GROUP BY status ORDER BY 2 DESC"
    ).fetchall()
    sections.append(_table("후보 상태", ("status", "count"), [tuple(r) for r in rows]))

    rows = conn.execute(
        "SELECT metric, value FROM daily_stats WHERE stat_date = ? ORDER BY metric", (date_str,)
    ).fetchall()
    sections.append(_table("오늘 통계", ("metric", "value"), [tuple(r) for r in rows]))

    rows = conn.execute(
        "SELECT a.action_type, c.username, ROUND(a.target_score,1), a.status, "
        "COALESCE(a.comment_text,''), a.updated_at "
        "FROM action_queue a JOIN creators c ON c.creator_id = a.creator_id "
        "ORDER BY a.action_id DESC LIMIT 20"
    ).fetchall()
    sections.append(
        _table(
            "최근 Action Queue (20)",
            ("type", "creator", "score", "status", "comment", "updated"),
            [tuple(r) for r in rows],
        )
    )

    rows = conn.execute(
        "SELECT i.action_type, c.username, i.dry_run, i.success, i.executed_at "
        "FROM interactions i JOIN creators c ON c.creator_id = i.creator_id "
        "ORDER BY i.interaction_id DESC LIMIT 20"
    ).fetchall()
    sections.append(
        _table(
            "최근 Interaction (20)",
            ("type", "creator", "dry_run", "success", "executed_at"),
            [tuple(r) for r in rows],
        )
    )

    return PAGE_TEMPLATE.format(
        db=html.escape(str(db_path)), date=html.escape(date_str), sections="".join(sections)
    )


def serve(config: Config) -> None:
    """Dashboard를 로컬에서 실행한다(Ctrl+C로 종료)."""
    host = str(config.get("dashboard.host", "127.0.0.1"))
    port = int(config.get("dashboard.port", 8501))
    db_path = config.db_path
    tz_offset = int(config.get("actions.daily_limits.timezone_offset_hours", 9))

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - http.server 규약
            if self.path not in ("/", "/index.html"):
                self.send_error(404)
                return
            conn = get_connection(db_path)
            try:
                page = render_page(conn, db_path, today_str(tz_offset))
            finally:
                conn.close()
            payload = page.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
            logger.debug("dashboard %s", fmt % args)

    server = HTTPServer((host, port), Handler)
    logger.info("Dashboard 실행: http://%s:%d (종료: Ctrl+C)", host, port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:  # pragma: no cover - 사용자 종료
        logger.info("Dashboard를 종료합니다.")
    finally:
        server.server_close()


if __name__ == "__main__":  # pragma: no cover
    serve(load_config())
