"""운영 Dashboard (Phase 14) — 표준 라이브러리 http.server 기반.

한 화면에서: 후보 확인 → 분석 확인 → 댓글 선택/수정 → Action 승인 → Skip/Feedback.

지키는 것:
- **127.0.0.1 전용.** 외부 인터페이스 바인딩 요청은 실행을 거부한다.
- **Claude Runtime을 호출하지 않는다.** DB에 저장된 분석 결과만 표시한다.
- **Instagram Action을 실행하지 않는다.** Action Queue와 Feedback만 관리한다.
- POST는 실행 중 생성한 토큰을 확인한다(브라우저 밖 요청/오조작 방지).

실행: python -m targeting_agent.main --dashboard  (또는 run_targeting_dashboard.bat)
"""
from __future__ import annotations

import html
import secrets
import sqlite3
import urllib.parse
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Optional, Sequence

from ..core.config import Config, load_config
from ..core.database import get_connection, init_db
from ..core.exceptions import ConfigError
from ..core.logger import get_logger, setup_logging
from ..core.models import ActionType, FeedbackType, MediaStatus
from . import queries
from .service import DashboardService

logger = get_logger("dashboard")

ALLOWED_HOSTS = {"127.0.0.1", "localhost", "::1"}
FEEDBACK_BUTTONS = (
    (FeedbackType.GOOD_TARGET, "좋은 Target"),
    (FeedbackType.NOT_MY_STYLE, "관심 없음"),
    (FeedbackType.GOOD_COMMENT, "좋은 댓글"),
    (FeedbackType.BAD_COMMENT, "나쁜 댓글"),
    (FeedbackType.RESPONDED, "응답 있음"),
    (FeedbackType.FOLLOWED, "팔로우됨"),
)

STYLE = """
body{font-family:system-ui,"Malgun Gothic",sans-serif;margin:0;padding:20px 24px;color:#1b1b1b;background:#fafafa}
h1{font-size:19px;margin:0 0 4px} h2{font-size:15px;margin:22px 0 8px}
a{color:#0b5fff} .muted{color:#666;font-size:12px}
.tabs{margin:12px 0 18px;border-bottom:1px solid #ddd}
.tabs a{display:inline-block;padding:8px 14px;text-decoration:none;border:1px solid transparent;border-bottom:none}
.tabs a.on{background:#fff;border-color:#ddd;border-radius:6px 6px 0 0;font-weight:600;color:#1b1b1b}
.grid{display:flex;flex-wrap:wrap;gap:10px;margin-bottom:6px}
.stat{background:#fff;border:1px solid #e3e3e3;border-radius:8px;padding:8px 12px;min-width:104px}
.stat b{display:block;font-size:17px}
.card{background:#fff;border:1px solid #e3e3e3;border-radius:8px;padding:12px 14px;margin-bottom:10px}
.row{display:flex;justify-content:space-between;gap:12px;align-items:flex-start}
.score{font-size:20px;font-weight:700}
.badge{display:inline-block;font-size:11px;padding:1px 7px;border-radius:10px;background:#eef;margin-left:6px}
.badge.h{background:#f1f1f1} .badge.w{background:#fff0e0} .badge.s{background:#ffecec}
table{border-collapse:collapse;width:100%;font-size:13px;background:#fff}
th,td{border:1px solid #e3e3e3;padding:5px 8px;text-align:left} th{background:#f5f5f5}
button{padding:6px 12px;border:1px solid #ccc;border-radius:6px;background:#fff;cursor:pointer;font-size:13px}
button.p{background:#0b5fff;color:#fff;border-color:#0b5fff}
input[type=text]{width:100%;padding:6px;border:1px solid #ccc;border-radius:6px;font-size:13px}
form.inline{display:inline}
.notice{background:#eef6ff;border:1px solid #b9d6ff;padding:8px 12px;border-radius:6px;margin-bottom:12px}
.bars div{display:flex;gap:8px;align-items:center;font-size:12px;margin:2px 0}
.bars span.n{width:92px;color:#555} .bars span.v{width:38px;text-align:right}
.bar{height:7px;background:#d9e6ff;border-radius:4px}
"""


@dataclass
class Page:
    """렌더링에 필요한 요청 컨텍스트."""

    tab: str = "review"
    media_pk: Optional[int] = None
    message: str = ""
    filters: dict[str, str] = None  # type: ignore[assignment]


def validate_host(host: str) -> str:
    """127.0.0.1/localhost 외의 바인딩은 거부한다."""
    normalized = (host or "").strip()
    if normalized not in ALLOWED_HOSTS:
        raise ConfigError(
            f"Dashboard는 127.0.0.1에서만 실행합니다(요청된 host: {normalized or '(빈 값)'}). "
            "외부 인터페이스 바인딩은 지원하지 않습니다."
        )
    return normalized


def e(value: Any) -> str:
    return html.escape("" if value is None else str(value))


# --- 렌더링 --------------------------------------------------------------
def _tabs(active: str) -> str:
    items = (("review", "Review"), ("queue", "Action Queue"), ("stats", "Feedback / Stats"))
    links = "".join(
        f'<a class="{"on" if key == active else ""}" href="/?tab={key}">{label}</a>'
        for key, label in items
    )
    return f'<div class="tabs">{links}</div>'


def _summary(conn: sqlite3.Connection, config: Config) -> str:
    tz = int(config.get("actions.daily_limits.timezone_offset_hours", 9))
    s = queries.daily_summary(conn, tz)
    usage = queries.daily_action_usage(conn, tz, config.dry_run)
    like_limit = int(config.get("actions.daily_limits.likes", 0))
    comment_limit = int(config.get("actions.daily_limits.comments", 0))
    claude_limit = int(config.get("ai.daily_request_limit", 0))

    cells = [
        ("오늘 발견", s["discovered"]), ("분석 완료", s["analyzed"]),
        ("검토 대기", s["pending_review"]), ("승인", s["approved"]), ("Skip", s["skipped"]),
        ("LIKE Queue", s["like_queue"]), ("COMMENT Queue", s["comment_queue"]),
        ("LIKE 사용", f"{usage.get('LIKE', 0)} / {like_limit}"),
        ("COMMENT 사용", f"{usage.get('COMMENT', 0)} / {comment_limit}"),
        ("Claude 호출", f"{s['claude_calls']} / {claude_limit}"),
        ("Claude 분석", s["claude_analyzed"]), ("Heuristic", s["heuristic_analyzed"]),
    ]
    tiles = "".join(f'<div class="stat">{e(k)}<b>{e(v)}</b></div>' for k, v in cells)
    return f'<div class="grid">{tiles}</div><p class="muted">기준일 {e(s["date"])} · 저장된 분석 결과만 표시합니다(Claude를 호출하지 않음).</p>'


def _add_form(token: str) -> str:
    """새 Candidate 추가 — URL 하나만 붙여넣으면 되도록 구성한다."""
    return f"""
<div class="card">
  <h2 style="margin-top:0">새 Candidate 추가</h2>
  <form method="post" action="/add">
    <input type="hidden" name="token" value="{e(token)}">
    <input type="text" name="url" placeholder="https://www.instagram.com/reel/XXXX/"
           maxlength="500" autofocus>
    <details style="margin:8px 0">
      <summary class="muted">선택 정보 (username · caption · note)</summary>
      <p class="muted" style="margin:6px 0 2px">
        캡션이나 username을 넣으면 바로 분석할 수 있습니다. 비워 두면 정보 부족 상태로 등록됩니다.
      </p>
      <input type="text" name="username" placeholder="username (선택)" maxlength="100">
      <input type="text" name="caption" placeholder="caption (선택)" maxlength="2000"
             style="margin-top:6px">
      <input type="text" name="note" placeholder="note (선택)" maxlength="500"
             style="margin-top:6px">
    </details>
    <button class="p" type="submit" name="mode" value="analyze">추가 후 분석</button>
    <button type="submit" name="mode" value="add">추가만</button>
  </form>
</div>
"""


def _filter_form(filters: dict[str, str]) -> str:
    def options(name: str, values: Sequence[tuple[str, str]]) -> str:
        current = filters.get(name, "")
        items = "".join(
            f'<option value="{e(v)}"{" selected" if v == current else ""}>{e(label)}</option>'
            for v, label in values
        )
        return f'<select name="{name}">{items}</select>'

    return (
        '<form method="get" class="card"><input type="hidden" name="tab" value="review">'
        "상태 " + options("status", [("", "검토 대기"), ("QUEUED", "승인"), ("SKIPPED", "Skip"), ("ALL", "전체")])
        + " 소스 " + options("source", [("", "전체"), ("manual_url", "manual_url"), ("csv_inbox", "csv_inbox"), ("import", "import"), ("hashtag", "hashtag")])
        + " 분석 " + options("analyzer", [("", "전체"), ("claude_code", "Claude"), ("heuristic", "Heuristic")])
        + f' 점수 <input type="text" name="min_score" value="{e(filters.get("min_score", ""))}" style="width:56px"> ~ '
        + f'<input type="text" name="max_score" value="{e(filters.get("max_score", ""))}" style="width:56px"> '
        + '<button type="submit">적용</button></form>'
    )


def _candidate_cards(conn: sqlite3.Connection, filters: dict[str, str]) -> str:
    status = filters.get("status", "")
    statuses = None if not status else (None if status == "ALL" else [status])
    if status == "ALL":
        statuses = [s.value for s in MediaStatus]
    rows = queries.list_candidates(
        conn,
        statuses=statuses,
        source=filters.get("source") or None,
        analyzer=filters.get("analyzer") or None,
        min_score=_as_float(filters.get("min_score")),
        max_score=_as_float(filters.get("max_score")),
    )
    if not rows:
        return '<p class="muted">조건에 맞는 후보가 없습니다.</p>'

    cards = []
    for row in rows:
        payload = queries._loads(row["payload"])
        label = queries.analyzer_label(row["analyzer"])
        badge_class = "" if label == "Claude" else ("h" if label == "Heuristic" else "w")
        summary = payload.get("summary") or (row["caption"] or "")[:60]
        if row["status"] == MediaStatus.NEEDS_ENRICHMENT.value:
            summary = "URL만 등록되어 분석에 필요한 정보가 없습니다(정보 부족)."
        topics = ", ".join(payload.get("topics") or [])
        score = row["target_score"]
        cards.append(
            f'<div class="card"><div class="row"><div>'
            f'<b>@{e(row["username"])}</b><span class="badge {badge_class}">{e(label)}</span>'
            f'<span class="badge h">{e(row["status"])}</span>'
            f'<span class="badge h">{e(row["source"])}</span>'
            f'<div class="muted">{e(topics)}</div><div>{e(summary)}</div></div>'
            f'<div style="text-align:right"><div class="score">{"-" if score is None else f"{score:.0f}"}</div>'
            f'<a href="/?tab=review&media={row["media_pk"]}">검토</a></div></div></div>'
        )
    return "".join(cards)


def _score_bars(breakdown: dict[str, Any]) -> str:
    components = breakdown.get("components") or {}
    if not components:
        return '<p class="muted">점수 구성 정보가 없습니다.</p>'
    rows = []
    for name, value in components.items():
        percent = max(0.0, min(1.0, float(value))) * 100
        rows.append(
            f'<div><span class="n">{e(name)}</span>'
            f'<span class="bar" style="width:{percent:.0f}px"></span>'
            f'<span class="v">{percent:.0f}</span></div>'
        )
    notes = ", ".join(breakdown.get("notes") or [])
    return f'<div class="bars">{"".join(rows)}</div><p class="muted">{e(notes)}</p>'


def _detail(conn: sqlite3.Connection, config: Config, media_pk: int, token: str) -> str:
    detail = queries.candidate_detail(conn, media_pk)
    if detail is None:
        return '<p class="muted">후보를 찾을 수 없습니다.</p>'

    analysis = detail["analysis"]
    label = queries.analyzer_label(detail["analyzer"])
    permalink = detail["permalink"]
    hidden = f'<input type="hidden" name="token" value="{e(token)}">' \
             f'<input type="hidden" name="media" value="{media_pk}">'

    comments = "".join(
        f'<label style="display:block;margin:3px 0"><input type="radio" name="draft_id" '
        f'value="{row["draft_id"]}"{" checked" if row["status"] == "SELECTED" else ""}> '
        f'{e(row["text"])} <span class="badge h">{e(row["generator"])}</span></label>'
        for row in detail["comments"]
    ) or '<p class="muted">생성된 댓글 후보가 없습니다.</p>'

    selected_text = next(
        (row["text"] for row in detail["comments"] if row["status"] == "SELECTED"), ""
    )
    interactions = "".join(
        f'<li>{e(row["executed_at"])} {e(row["action_type"])}'
        f'{" (dry-run)" if row["dry_run"] else ""}</li>'
        for row in detail["interactions"]
    ) or "<li class='muted'>없음</li>"
    actions = "".join(
        f'<li>{e(row["action_type"])} — {e(row["status"])}'
        f'{" · 승인 " + e(row["approved_at"]) if row["approved_at"] else ""}</li>'
        for row in detail["actions"]
    ) or "<li class='muted'>없음</li>"
    feedback_done = set(detail["feedback"])
    feedback_buttons = "".join(
        f'<form method="post" action="/feedback" class="inline">{hidden}'
        f'<input type="hidden" name="feedback" value="{f.value}">'
        f'<button{" disabled" if f.value in feedback_done else ""}>{e(text)}'
        f'{" ✓" if f.value in feedback_done else ""}</button></form> '
        for f, text in FEEDBACK_BUTTONS
    )

    relevance = analysis.get("relevance_score")
    meta = (
        f'분석 {e(label)} · model {e(config.get("ai.model", "-"))} · '
        f'prompt {e(detail["prompt_version"])} · analyzer_version {e(detail["analyzer_version"])}'
    )
    fallback_note = ""
    if label == "Heuristic" and relevance is None:
        fallback_note = '<span class="badge w">Claude 결과 없음 — heuristic 사용</span>'

    return f"""
<div class="card">
  <div class="row">
    <div><b>@{e(detail["username"])}</b>
      <span class="badge h">{e(detail["status"])}</span>
      <span class="badge h">{e(detail["source"])}</span> {fallback_note}
      <div class="muted">{meta}</div>
    </div>
    <div style="text-align:right"><div class="score">
      {"-" if detail["target_score"] is None else f'{detail["target_score"]:.0f}'}</div>
      <a href="{e(permalink)}" target="_blank" rel="noopener noreferrer">Instagram에서 열기</a>
    </div>
  </div>
  <p>{e(detail["caption"] or "(캡션 없음)")}</p>
  <p class="muted">해시태그: {e(", ".join(detail["hashtags"]))}</p>
  <p class="muted">요약: {e(analysis.get("summary") or "-")} · 주제: {e(analysis.get("primary_topic") or "-")}
     · 분위기: {e(analysis.get("mood") or "-")} · 언어: {e(analysis.get("language") or "-")}</p>
  <p class="muted">관련성: {e(relevance if relevance is not None else "-")}
     — {e(analysis.get("relevance_reason") or "저장된 사유 없음")}</p>
  {_score_bars(detail["breakdown"])}
</div>

<div class="card"><h2>댓글 후보</h2>
  <form method="post" action="/select-comment">{hidden}
    {comments}
    <p class="muted">직접 수정해서 쓰려면 아래에 입력하세요(원본 후보는 보존됩니다).</p>
    <input type="text" name="text" value="{e(selected_text)}" placeholder="수정한 댓글">
    <p><button class="p" type="submit">댓글 선택 저장</button></p>
  </form>
</div>

<div class="card"><h2>Action</h2>
  <form method="post" action="/approve">{hidden}
    <label><input type="checkbox" name="like" checked> LIKE</label>
    <label style="margin-left:12px"><input type="checkbox" name="comment"> COMMENT</label>
    <p class="muted">선택한 Action을 Queue에 올립니다. 이 화면에서 Instagram 동작을 실행하지 않습니다.</p>
    <button class="p" type="submit">Action 승인</button>
  </form>
  <form method="post" action="/skip" class="inline">{hidden}<button>Skip</button></form>
  <form method="post" action="/reopen" class="inline">{hidden}<button>검토 대기로 되돌리기</button></form>
  <h2>이 후보의 Action</h2><ul>{actions}</ul>
  <h2>최근 Interaction</h2><ul>{interactions}</ul>
</div>

<div class="card"><h2>Feedback</h2>{feedback_buttons}</div>
"""


def _queue_tab(conn: sqlite3.Connection) -> str:
    rows = queries.list_actions(conn)
    if not rows:
        return '<p class="muted">Action이 없습니다.</p>'
    def label(row: Any) -> str:
        if row["status"] != "PENDING":
            return str(row["status"])
        return "실행 대기(승인됨)" if row["approved_at"] else "승인 대기"

    body = "".join(
        f"<tr><td>@{e(r['username'])}</td><td>{e(r['action_type'])}</td>"
        f"<td>{e(r['comment_text'] or '')}</td><td>{float(r['target_score'] or 0):.0f}</td>"
        f"<td>{e(r['created_at'])}</td><td>{e(r['approved_at'] or '-')}</td>"
        f"<td>{e(label(r))}</td></tr>"
        for r in rows
    )
    return (
        "<table><tr><th>Creator</th><th>Action</th><th>Comment</th><th>Score</th>"
        f"<th>Created</th><th>Approved</th><th>Status</th></tr>{body}</table>"
        '<p class="muted">실행은 run_targeting.bat에서 수행합니다. 이 화면에는 실행 버튼이 없습니다.</p>'
    )


def _stats_tab(conn: sqlite3.Connection, config: Config) -> str:
    data = queries.feedback_summary(conn)
    counts = "".join(
        f'<div class="stat">{e(k)}<b>{e(v)}</b></div>' for k, v in sorted(data["counts"].items())
    ) or '<p class="muted">Feedback이 없습니다.</p>'
    recent = "".join(
        f"<tr><td>{e(r['created_at'])}</td><td>{e(r['feedback_type'])}</td>"
        f"<td>@{e(r['username'] or '-')}</td><td>{e(r['note'] or '')}</td></tr>"
        for r in data["recent"]
    )
    table = (
        f"<table><tr><th>시각</th><th>Feedback</th><th>Creator</th><th>메모</th></tr>{recent}</table>"
        if recent else ""
    )
    return (
        f'<h2>Feedback 누적</h2><div class="grid">{counts}</div>'
        f"<h2>최근 Feedback</h2>{table}"
        '<p class="muted">응답 있음 / 팔로우됨은 현재 수동 입력입니다(자동 감지는 Phase 16 범위).</p>'
    )


def render(conn: sqlite3.Connection, config: Config, page: Page, token: str) -> str:
    filters = page.filters or {}
    if page.tab == "queue":
        body = _queue_tab(conn)
    elif page.tab == "stats":
        body = _stats_tab(conn, config)
    elif page.media_pk is not None:
        body = (
            f'<p><a href="/?tab=review">← 목록으로</a></p>'
            + _detail(conn, config, page.media_pk, token)
        )
    else:
        body = _add_form(token) + _filter_form(filters) + _candidate_cards(conn, filters)

    notice = f'<div class="notice">{e(page.message)}</div>' if page.message else ""
    return (
        '<!DOCTYPE html><html lang="ko"><head><meta charset="utf-8">'
        "<title>DailyReels Targeting Agent</title>"
        f"<style>{STYLE}</style></head><body>"
        "<h1>DailyReels Targeting Agent</h1>"
        '<p class="muted">운영 화면 — Instagram 동작을 실행하지 않고 Action Queue와 Feedback만 관리합니다.</p>'
        f"{_tabs(page.tab)}{notice}{_summary(conn, config)}{body}</body></html>"
    )


def _as_float(value: Any) -> Optional[float]:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


# --- 서버 ----------------------------------------------------------------
def serve(config: Config) -> None:
    """Dashboard를 로컬에서 실행한다(Ctrl+C로 종료)."""
    host = validate_host(str(config.get("dashboard.host", "127.0.0.1")))
    port = int(config.get("dashboard.port", 8501))
    token = secrets.token_urlsafe(16)
    db_path = config.db_path

    def open_conn() -> sqlite3.Connection:
        conn = get_connection(db_path)
        init_db(conn)
        return conn

    class Handler(BaseHTTPRequestHandler):
        server_version = "TargetingDashboard/1.0"

        def do_GET(self) -> None:  # noqa: N802
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path not in ("/", "/index.html"):
                self.send_error(404)
                return
            params = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}
            page = Page(
                tab=params.get("tab", "review"),
                media_pk=int(params["media"]) if params.get("media", "").isdigit() else None,
                message=params.get("msg", ""),
                filters={
                    k: params.get(k, "")
                    for k in ("status", "source", "analyzer", "min_score", "max_score")
                },
            )
            conn = open_conn()
            try:
                body = render(conn, config, page, token)
            finally:
                conn.close()
            self._send_html(body)

        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length).decode("utf-8") if length else ""
            form = {k: v[0] for k, v in urllib.parse.parse_qs(raw).items()}

            if form.get("token") != token:
                self.send_error(403, "invalid token")
                return

            path = urllib.parse.urlparse(self.path).path
            conn = open_conn()
            try:
                if path == "/add":
                    result = DashboardService(conn, config).add_candidate(
                        form.get("url", ""),
                        username=form.get("username", ""),
                        caption=form.get("caption", ""),
                        note=form.get("note", ""),
                        analyze=form.get("mode") == "analyze",
                    )
                    query = f"tab=review&msg={urllib.parse.quote(result.message)}"
                    if result.reviewable:
                        query += f"&media={result.media_pk}"
                    self._redirect(f"/?{query}")
                    return

                media_pk = int(form.get("media", "0") or 0)
                if not media_pk:
                    self.send_error(400, "media required")
                    return
                message = self._handle(conn, path, media_pk, form)
            finally:
                conn.close()
            target = f"/?tab=review&media={media_pk}&msg={urllib.parse.quote(message)}"
            self._redirect(target)

        def _redirect(self, target: str) -> None:
            self.send_response(303)
            self.send_header("Location", target)
            self.end_headers()

        def _handle(
            self, conn: sqlite3.Connection, path: str, media_pk: int, form: dict[str, str]
        ) -> str:
            service = DashboardService(conn, config)
            if path == "/select-comment":
                draft_id = int(form["draft_id"]) if form.get("draft_id", "").isdigit() else None
                saved = service.select_comment(media_pk, draft_id=draft_id, text=form.get("text"))
                return "댓글을 선택했습니다." if saved else "선택할 댓글이 없습니다."
            if path == "/approve":
                types = []
                if form.get("like"):
                    types.append(ActionType.LIKE)
                if form.get("comment"):
                    types.append(ActionType.COMMENT)
                if not types:
                    return "선택한 Action이 없습니다."
                return service.approve(media_pk, types).message
            if path == "/skip":
                service.skip(media_pk)
                return "Skip 처리했습니다."
            if path == "/reopen":
                return service.reopen(media_pk)[1]
            if path == "/feedback":
                try:
                    feedback = FeedbackType(str(form.get("feedback")))
                except ValueError:
                    return "알 수 없는 Feedback입니다."
                return service.add_feedback(media_pk, feedback)[1]
            return "알 수 없는 요청입니다."

        def _send_html(self, body: str) -> None:
            payload = body.encode("utf-8")
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
    configuration = load_config()
    setup_logging(level=str(configuration.get("logging.level", "INFO")))
    serve(configuration)
