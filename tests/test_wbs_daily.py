"""Daily To-Do orchestration + persistence + JSON + CLI + FOLLOW_UP 테스트 (Commit 5).

Google API / Claude / Slack 실호출 없음 — FakeSheetsClient + today 주입.
"""
import datetime as dt
import inspect
import json
import sys

import pytest

from engine.config.loader import DriveConfig, ProjectConfig
from engine.todo.daily import run_daily_todo
from engine.todo.persist import get_daily_items, get_daily_run
from engine.wbs.config import WBSConfig
from engine.wbs.state import load_states
from engine.wbs.sync import WBSBootstrapRequired, run_wbs_bootstrap
from tests.fakes import FakeSheetsClient

TODAY = dt.date(2026, 9, 10)
HEADER = ["ID", "Task", "Owner", "Start", "End", "Progress", "Status", "Notes"]
_ORDER = ["id", "task", "owner", "start", "end", "progress", "status", "notes"]


def rows(*tasks):
    out = [list(HEADER)]
    for task in tasks:
        row = []
        for key in _ORDER:
            if key == "status":
                row.append(task.get("status", "진행"))  # 기본 IN_PROGRESS (미지정 시 UNKNOWN 방지)
            else:
                row.append(task.get(key, ""))
        out.append(row)
    return out


def overdue(dref=TODAY, delta=1):
    return (dref - dt.timedelta(days=delta)).isoformat()


def future(days=60):
    return (TODAY + dt.timedelta(days=days)).isoformat()


def make_wbs_config(**overrides):
    raw = {
        "spreadsheet_id": "SHEET1",
        "sheet_name": "WBS",
        "timezone": "Asia/Seoul",
        "due_soon_days": 3,
        "progress_done_threshold": 1.0,
        "progress_change_min_delta": 0.1,
        "columns": {
            "task_id": "ID", "task_name": "Task", "owner": "Owner", "start_date": "Start",
            "end_date": "End", "progress": "Progress", "status": "Status", "notes": "Notes",
        },
        "status_map": {"DONE": ["완료"], "BLOCKED": ["지연"], "IN_PROGRESS": ["진행"]},
        "blocked_keywords": ["차단"],
        "change_significant_fields": ["start_date", "end_date", "owner", "normalized_status", "progress"],
    }
    raw.update(overrides)
    return WBSConfig.from_raw(raw)


def make_config(wbs="__default__"):
    return ProjectConfig(
        project_id="demo",
        project_name="Demo",
        drive=DriveConfig("my_drive", "ROOT", True, []),
        scope_config_version=1,
        wbs=make_wbs_config() if wbs == "__default__" else wbs,
    )


class SeqIds:
    def __init__(self, prefix="t"):
        self.n = 0
        self.prefix = prefix

    def __call__(self):
        self.n += 1
        return f"{self.prefix}{self.n}"


def client_with(modified, *tasks):
    client = FakeSheetsClient()
    client.set_modified_time(modified)
    client.set_values(rows(*tasks))
    return client


def bootstrap(conn, client, ids=None, config=None):
    return run_wbs_bootstrap(conn, config or make_config(), client, id_factory=ids or SeqIds())


def daily(conn, client, *, today=TODAY, ids=None, config=None):
    return run_daily_todo(conn, config or make_config(), client, today=today, id_factory=ids or SeqIds())


def items(conn, date=TODAY):
    return get_daily_items(conn, "demo", date.isoformat())


def run_row(conn, date=TODAY):
    return get_daily_run(conn, "demo", date.isoformat())


# --- 1~3: 기본 / today / prerequisite ------------------------------
def test_basic_daily_run_creates_json_and_rows(conn):
    ids = SeqIds()
    client = client_with("m1", {"id": "W-1", "task": "지연작업", "end": overdue(delta=2)})
    bootstrap(conn, client, ids)
    result = run_daily_todo(conn, make_config(), client, today=TODAY, id_factory=ids)

    out = result.output
    assert out["schema_version"] == "1.0"
    assert out["date"] == "2026-09-10"
    assert out["timezone"] == "Asia/Seoul"
    assert out["run"]["source_synced"] is False
    assert out["run"]["status"] == "success"
    assert [t["category"] for t in out["todos"]] == ["OVERDUE"]

    assert run_row(conn)["source_synced"] == 0
    assert run_row(conn)["status"] == "success"
    assert [i["internal_task_id"] for i in items(conn)] == ["t1"]
    assert result.run_id == run_row(conn)["run_id"]


def test_today_injection_overrides_system_clock(conn):
    ids = SeqIds()
    client = client_with("m1", {"id": "W-1", "task": "A", "end": "2000-01-01"})
    bootstrap(conn, client, ids)
    out = run_daily_todo(conn, make_config(), client, today=dt.date(2000, 1, 2), id_factory=ids).output
    assert out["date"] == "2000-01-02"
    assert out["todos"][0]["category"] == "OVERDUE"


def test_daily_does_not_auto_bootstrap(conn):
    with pytest.raises(WBSBootstrapRequired):
        run_daily_todo(conn, make_config(), FakeSheetsClient(), today=TODAY)
    assert load_states(conn, "demo", "SHEET1", "WBS") == []
    assert run_row(conn) is None


# --- 4~5: source sync -------------------------------------------
def test_source_changed_sets_source_synced_true(conn):
    ids = SeqIds()
    client = client_with("m1", {"id": "W-1", "task": "A", "end": overdue()})
    bootstrap(conn, client, ids)
    client.set_modified_time("m2")
    client.set_values(rows(
        {"id": "W-1", "task": "A", "end": overdue()},
        {"id": "W-2", "task": "B", "end": overdue()},
    ))
    out = run_daily_todo(conn, make_config(), client, today=TODAY, id_factory=ids).output
    assert out["run"]["source_synced"] is True
    assert len(out["todos"]) == 2


def test_source_unchanged_skips_get_values(conn):
    ids = SeqIds()
    client = client_with("m1", {"id": "W-1", "task": "A", "end": overdue()})
    bootstrap(conn, client, ids)
    calls = client.get_values_calls
    out = run_daily_todo(conn, make_config(), client, today=TODAY, id_factory=ids).output
    assert client.get_values_calls == calls
    assert out["run"]["source_synced"] is False


# --- 6~8: CHANGED via before/after snapshot -----------------
def test_changed_end_date_creates_changed_candidate(conn):
    ids = SeqIds()
    client = client_with("m1", {"id": "W-1", "task": "A", "end": future(30)})
    bootstrap(conn, client, ids)
    client.set_modified_time("m2")
    client.set_values(rows({"id": "W-1", "task": "A", "end": future(40)}))
    out = run_daily_todo(conn, make_config(), client, today=TODAY, id_factory=ids).output
    assert [t["category"] for t in out["todos"]] == ["CHANGED"]
    assert "end_date" in out["todos"][0]["changed_fields"]


def test_changed_progress_below_delta_is_not_changed(conn):
    ids = SeqIds()
    client = client_with("m1", {"id": "W-1", "task": "A", "end": future(30), "progress": "50%"})
    bootstrap(conn, client, ids)
    client.set_modified_time("m2")
    client.set_values(rows({"id": "W-1", "task": "A", "end": future(30), "progress": "55%"}))
    out = run_daily_todo(conn, make_config(), client, today=TODAY, id_factory=ids).output
    assert out["todos"] == []


def test_changed_progress_above_delta_is_changed(conn):
    ids = SeqIds()
    client = client_with("m1", {"id": "W-1", "task": "A", "end": future(30), "progress": "50%"})
    bootstrap(conn, client, ids)
    client.set_modified_time("m2")
    client.set_values(rows({"id": "W-1", "task": "A", "end": future(30), "progress": "85%"}))
    out = run_daily_todo(conn, make_config(), client, today=TODAY, id_factory=ids).output
    assert [t["category"] for t in out["todos"]] == ["CHANGED"]


def test_new_task_is_not_changed(conn):
    ids = SeqIds()
    client = client_with("m1", {"id": "W-1", "task": "A", "end": future(30)})
    bootstrap(conn, client, ids)
    client.set_modified_time("m2")
    client.set_values(rows(
        {"id": "W-1", "task": "A", "end": future(30)},
        {"id": "W-2", "task": "B", "end": future(30)},
    ))
    out = run_daily_todo(conn, make_config(), client, today=TODAY, id_factory=ids).output
    assert out["todos"] == []  # 신규는 CHANGED 대상 아님, 다른 category 도 없음


# --- 9~13: FOLLOW_UP ------------------------------------------
def _seed_day1_start_today(conn, ids):
    d1 = TODAY - dt.timedelta(days=1)
    client = client_with("m1", {"id": "W-1", "task": "A", "start": d1.isoformat(), "end": future(60)})
    bootstrap(conn, client, ids)
    r1 = run_daily_todo(conn, make_config(), client, today=d1, id_factory=ids)
    assert r1.output["todos"][0]["category"] == "START_TODAY"
    return client


def test_follow_up_standalone(conn):
    ids = SeqIds()
    client = _seed_day1_start_today(conn, ids)
    out = run_daily_todo(conn, make_config(), client, today=TODAY, id_factory=ids).output
    assert [t["category"] for t in out["todos"]] == ["FOLLOW_UP"]
    assert "미해결" in out["todos"][0]["reason"]
    assert out["summary"]["follow_up"] == 1


def test_follow_up_merged_into_overdue(conn):
    ids = SeqIds()
    d1 = TODAY - dt.timedelta(days=1)
    client = client_with("m1", {"id": "W-1", "task": "A", "end": d1.isoformat()})
    bootstrap(conn, client, ids)
    run_daily_todo(conn, make_config(), client, today=d1, id_factory=ids)  # DUE_TODAY on d1
    out = run_daily_todo(conn, make_config(), client, today=TODAY, id_factory=ids).output
    todo = out["todos"][0]
    assert todo["category"] == "OVERDUE"
    assert "FOLLOW_UP" in todo["also_categories"]
    assert "미해결" in todo["reason"]
    assert out["summary"]["follow_up"] == 1


def test_follow_up_excluded_when_now_done(conn):
    ids = SeqIds()
    d1 = TODAY - dt.timedelta(days=1)
    client = _seed_day1_start_today(conn, ids)
    client.set_modified_time("m2")
    client.set_values(rows({"id": "W-1", "task": "A", "start": d1.isoformat(), "end": future(60), "status": "완료"}))
    out = run_daily_todo(conn, make_config(), client, today=TODAY, id_factory=ids).output
    assert out["todos"] == []


def test_follow_up_excluded_when_now_inactive(conn):
    ids = SeqIds()
    client = _seed_day1_start_today(conn, ids)
    client.set_modified_time("m2")
    client.set_values(rows())  # W-1 삭제 → deactivated
    out = run_daily_todo(conn, make_config(), client, today=TODAY, id_factory=ids).output
    assert out["todos"] == []


def test_no_previous_items_means_no_follow_up(conn):
    ids = SeqIds()
    client = client_with("m1", {"id": "W-1", "task": "A", "end": future(60)})
    bootstrap(conn, client, ids)
    out = run_daily_todo(conn, make_config(), client, today=TODAY, id_factory=ids).output
    assert out["todos"] == []


# --- FOLLOW_UP continuity (기준일 = 오늘보다 이전인 가장 최근 실행일) ---------
def _far_from(d):
    return (d + dt.timedelta(days=90)).isoformat()


def test_follow_up_survives_skipped_day(conn):
    ids = SeqIds()
    d4, d6 = dt.date(2026, 9, 4), dt.date(2026, 9, 6)
    client = client_with("m1", {"id": "W-1", "task": "A", "start": d4.isoformat(), "end": _far_from(d6)})
    bootstrap(conn, client, ids)
    r4 = run_daily_todo(conn, make_config(), client, today=d4, id_factory=ids)
    assert r4.output["todos"][0]["category"] == "START_TODAY"
    # d5 실행 없음
    out = run_daily_todo(conn, make_config(), client, today=d6, id_factory=ids).output
    assert [t["category"] for t in out["todos"]] == ["FOLLOW_UP"]
    assert "2026-09-04" in out["todos"][0]["reason"]
    assert "전일" not in out["todos"][0]["reason"]


def test_follow_up_uses_last_run_across_weekend_gap(conn):
    ids = SeqIds()
    fri, mon = dt.date(2026, 9, 11), dt.date(2026, 9, 14)
    client = client_with("m1", {"id": "W-1", "task": "A", "start": fri.isoformat(), "end": _far_from(mon)})
    bootstrap(conn, client, ids)
    run_daily_todo(conn, make_config(), client, today=fri, id_factory=ids)
    out = run_daily_todo(conn, make_config(), client, today=mon, id_factory=ids).output
    assert [t["category"] for t in out["todos"]] == ["FOLLOW_UP"]
    assert "2026-09-11" in out["todos"][0]["reason"]


def test_follow_up_uses_only_most_recent_prior_run(conn):
    ids = SeqIds()
    d1, d2, d4 = dt.date(2026, 9, 1), dt.date(2026, 9, 2), dt.date(2026, 9, 4)
    client = client_with("m1", {"id": "W-1", "task": "A", "start": d1.isoformat(), "end": _far_from(d4)})
    bootstrap(conn, client, ids)
    run_daily_todo(conn, make_config(), client, today=d1, id_factory=ids)  # W-1 START_TODAY
    run_daily_todo(conn, make_config(), client, today=d2, id_factory=ids)  # W-1 FOLLOW_UP (from d1)
    # d3 실행 없음
    out = run_daily_todo(conn, make_config(), client, today=d4, id_factory=ids).output
    assert [t["category"] for t in out["todos"]] == ["FOLLOW_UP"]
    # 기준일은 09-01 이 아니라 가장 최근 이전 실행일 09-02
    assert "2026-09-02" in out["todos"][0]["reason"]


def test_follow_up_not_pulled_when_recent_run_has_no_unresolved_task(conn):
    ids = SeqIds()
    d1, d2, d3 = dt.date(2026, 9, 1), dt.date(2026, 9, 2), dt.date(2026, 9, 3)
    client = client_with("m1", {"id": "W-1", "task": "A", "start": d1.isoformat(), "end": _far_from(d3)})
    bootstrap(conn, client, ids)
    run_daily_todo(conn, make_config(), client, today=d1, id_factory=ids)  # W-1 START_TODAY -> d1 item
    run_daily_todo(conn, make_config(), client, today=d2, id_factory=ids)  # W-1 FOLLOW_UP -> d2 item
    # d2 의 W-1 을 resolved 처리 → 가장 최근 이전 실행일(d2) 의 unresolved 목록에 W-1 없음
    conn.execute(
        "UPDATE daily_todo_item SET resolved=1 WHERE project_id='demo' AND todo_date=? AND internal_task_id='t1'",
        (d2.isoformat(),),
    )
    conn.commit()
    # d3: d1 에는 W-1 이 unresolved 였지만 더 오래된 run 은 끌어오지 않는다.
    out = run_daily_todo(conn, make_config(), client, today=d3, id_factory=ids).output
    assert out["todos"] == []


def test_follow_up_ignores_failed_prior_run(conn):
    ids = SeqIds()
    d1 = TODAY - dt.timedelta(days=2)
    d_recent = TODAY - dt.timedelta(days=1)
    client = client_with("m1", {"id": "W-1", "task": "A", "start": d1.isoformat(), "end": _far_from(TODAY)})
    bootstrap(conn, client, ids)
    run_daily_todo(conn, make_config(), client, today=d1, id_factory=ids)  # W-1 START_TODAY -> d1 item
    # d_recent 에 failed run 을 수동 삽입 (item 없음)
    conn.execute(
        "INSERT INTO daily_todo_run (project_id, todo_date, status, created_at) VALUES ('demo', ?, 'failed', 't')",
        (d_recent.isoformat(),),
    )
    conn.commit()
    out = run_daily_todo(conn, make_config(), client, today=TODAY, id_factory=ids).output
    # failed(d_recent) 는 무시 → 기준일 d1 → W-1 FOLLOW_UP
    assert [t["category"] for t in out["todos"]] == ["FOLLOW_UP"]
    assert d1.isoformat() in out["todos"][0]["reason"]


def test_follow_up_pulled_from_needs_review_prior_run(conn):
    ids = SeqIds()
    d1 = TODAY - dt.timedelta(days=1)
    od = (d1 - dt.timedelta(days=1)).isoformat()
    tasks = [{"id": "W-1", "task": "AAA", "end": od}]
    tasks += [{"id": f"W-{i}", "task": f"T{i:03d}", "end": od} for i in range(2, 64)]  # 63 overdue
    client = client_with("m1", *tasks)
    bootstrap(conn, client, ids)
    r1 = run_daily_todo(conn, make_config(), client, today=d1, id_factory=ids)
    assert r1.output["run"]["status"] == "needs_review"
    assert any(t["internal_task_id"] == "t1" for t in r1.output["todos"])  # W-1(AAA)=t1 selected
    out = run_daily_todo(conn, make_config(), client, today=TODAY, id_factory=ids).output
    w1 = [t for t in out["todos"] if t["internal_task_id"] == "t1"][0]
    assert "FOLLOW_UP" in [w1["category"], *w1["also_categories"]]


# --- 14~17: same-day idempotency / resolved -----------------
def test_same_day_rerun_upserts_single_run_and_items(conn):
    ids = SeqIds()
    client = client_with("m1", {"id": "W-1", "task": "A", "end": overdue()})
    bootstrap(conn, client, ids)
    run_daily_todo(conn, make_config(), client, today=TODAY, id_factory=ids)
    run_daily_todo(conn, make_config(), client, today=TODAY, id_factory=ids)
    count = conn.execute(
        "SELECT COUNT(*) c FROM daily_todo_run WHERE project_id='demo' AND todo_date=?",
        (TODAY.isoformat(),),
    ).fetchone()["c"]
    assert count == 1
    assert len(items(conn)) == 1


def test_same_day_stale_item_is_deleted(conn):
    ids = SeqIds()
    client = client_with(
        "m1",
        {"id": "W-1", "task": "A", "end": overdue()},
        {"id": "W-2", "task": "B", "end": overdue()},
    )
    bootstrap(conn, client, ids)
    run_daily_todo(conn, make_config(), client, today=TODAY, id_factory=ids)
    assert len(items(conn)) == 2
    client.set_modified_time("m2")
    client.set_values(rows(
        {"id": "W-1", "task": "A", "end": overdue()},
        {"id": "W-2", "task": "B", "end": overdue(), "status": "완료"},
    ))
    run_daily_todo(conn, make_config(), client, today=TODAY, id_factory=ids)
    assert [i["internal_task_id"] for i in items(conn)] == ["t1"]


def test_same_day_rerun_preserves_resolved_flag(conn):
    ids = SeqIds()
    client = client_with("m1", {"id": "W-1", "task": "A", "end": overdue()})
    bootstrap(conn, client, ids)
    run_daily_todo(conn, make_config(), client, today=TODAY, id_factory=ids)
    conn.execute(
        "UPDATE daily_todo_item SET resolved=1 WHERE project_id='demo' AND todo_date=? AND internal_task_id='t1'",
        (TODAY.isoformat(),),
    )
    conn.commit()
    run_daily_todo(conn, make_config(), client, today=TODAY, id_factory=ids)
    row = conn.execute(
        "SELECT resolved FROM daily_todo_item WHERE internal_task_id='t1' AND todo_date=?",
        (TODAY.isoformat(),),
    ).fetchone()
    assert row["resolved"] == 1


def test_new_item_has_resolved_zero(conn):
    ids = SeqIds()
    client = client_with("m1", {"id": "W-1", "task": "A", "end": overdue()})
    bootstrap(conn, client, ids)
    run_daily_todo(conn, make_config(), client, today=TODAY, id_factory=ids)
    assert items(conn)[0]["resolved"] == 0


# --- 18~21: JSON / source_ref ------------------------------
def test_source_ref_persisted_as_json(conn):
    ids = SeqIds()
    client = client_with("m1", {"id": "W-1", "task": "A", "end": overdue()})
    bootstrap(conn, client, ids)
    run_daily_todo(conn, make_config(), client, today=TODAY, id_factory=ids)
    ref = json.loads(items(conn)[0]["source_ref"])
    assert ref["internal_task_id"] == "t1"
    assert ref["spreadsheet_id"] == "SHEET1"
    assert ref["sheet_name"] == "WBS"
    assert ref["row_key"] == "WBS!R2"
    assert "row_hash" in ref and ref["source_task_id"] == "W-1"


def test_json_includes_source_ref_and_schema_version(conn):
    ids = SeqIds()
    client = client_with("m1", {"id": "W-1", "task": "A", "end": overdue()})
    bootstrap(conn, client, ids)
    out = run_daily_todo(conn, make_config(), client, today=TODAY, id_factory=ids).output
    assert out["schema_version"] == "1.0"
    todo = out["todos"][0]
    assert set(todo["source_ref"]) == {
        "project_id", "spreadsheet_id", "sheet_name", "internal_task_id",
        "source_task_id", "row_key", "row_hash",
    }


def test_json_null_and_list_types_are_stable(conn):
    ids = SeqIds()
    client = client_with("m1", {"id": "W-1", "task": "A", "end": overdue()})
    bootstrap(conn, client, ids)
    out = run_daily_todo(conn, make_config(), client, today=TODAY, id_factory=ids).output
    todo = out["todos"][0]
    assert todo["owner"] is None
    assert todo["dependency"] == []
    assert todo["also_categories"] == []
    assert todo["changed_fields"] == []
    json.dumps(out)  # 직렬화 가능해야 함


def test_render_is_deterministic_except_generated_at(conn):
    ids = SeqIds()
    client = client_with(
        "m1",
        {"id": "W-1", "task": "B", "end": overdue()},
        {"id": "W-2", "task": "A", "end": overdue()},
    )
    bootstrap(conn, client, ids)
    o1 = run_daily_todo(conn, make_config(), client, today=TODAY, id_factory=ids).output
    o2 = run_daily_todo(conn, make_config(), client, today=TODAY, id_factory=ids).output
    o1.pop("generated_at")
    o2.pop("generated_at")
    assert o1 == o2


# --- 22~24: guardrail --------------------------------------
def test_guardrail_under_limit_success(conn):
    ids = SeqIds()
    tasks = [{"id": f"W-{i}", "task": f"T{i}", "end": overdue()} for i in range(10)]
    client = client_with("m1", *tasks)
    bootstrap(conn, client, ids)
    out = run_daily_todo(conn, make_config(), client, today=TODAY, id_factory=ids).output
    assert out["run"]["status"] == "success"
    assert out["run"]["guardrail_hit"] is None
    assert out["run"]["selected_count"] == 10
    assert out["run"]["omitted_count"] == 0


def test_guardrail_over_limit_needs_review_and_caps_items(conn):
    ids = SeqIds()
    tasks = [{"id": f"W-{i}", "task": f"T{i:03d}", "end": overdue()} for i in range(75)]
    client = client_with("m1", *tasks)
    bootstrap(conn, client, ids)
    result = run_daily_todo(conn, make_config(), client, today=TODAY, id_factory=ids)
    run = result.output["run"]
    assert run["status"] == "needs_review"
    assert run["guardrail_hit"] == "MAX_TODO_CANDIDATES"
    assert run["candidate_count"] == 75
    assert run["selected_count"] == 60
    assert run["omitted_count"] == 15
    assert len(items(conn)) == 60
    assert run_row(conn)["status"] == "needs_review"
    assert run_row(conn)["candidate_count"] == 75


def test_guardrail_selection_deterministic_across_runs(conn):
    ids = SeqIds()
    tasks = [{"id": f"W-{i}", "task": f"T{i:03d}", "end": overdue()} for i in range(70)]
    client = client_with("m1", *tasks)
    bootstrap(conn, client, ids)
    o1 = run_daily_todo(conn, make_config(), client, today=TODAY, id_factory=ids).output
    o2 = run_daily_todo(conn, make_config(), client, today=TODAY, id_factory=ids).output
    assert [t["internal_task_id"] for t in o1["todos"]] == [t["internal_task_id"] for t in o2["todos"]]


# --- 25~27: confirmation / duplicates / claude -------------
def test_confirmation_override_persisted(conn):
    ids = SeqIds()
    client = client_with("m1", {"id": "W-1", "task": "A", "status": "완료", "end": "bad-date"})
    bootstrap(conn, client, ids)
    out = run_daily_todo(conn, make_config(), client, today=TODAY, id_factory=ids).output
    assert out["todos"][0]["category"] == "NEEDS_CONFIRMATION"
    assert out["todos"][0]["needs_confirmation"] is True
    row = items(conn)[0]
    assert row["category"] == "NEEDS_CONFIRMATION"
    assert row["needs_confirmation"] == 1


def test_duplicate_source_task_id_keeps_two_todos(conn):
    ids = SeqIds()
    client = client_with(
        "m1",
        {"id": "W-1", "task": "A", "end": overdue()},
        {"id": "W-1", "task": "B", "end": overdue()},
    )
    bootstrap(conn, client, ids)
    out = run_daily_todo(conn, make_config(), client, today=TODAY, id_factory=ids).output
    assert len(out["todos"]) == 2
    assert {t["internal_task_id"] for t in out["todos"]} == {"t1", "t2"}
    assert {i["internal_task_id"] for i in items(conn)} == {"t1", "t2"}


def test_claude_passthrough_no_usage(conn):
    ids = SeqIds()
    client = client_with("m1", {"id": "W-1", "task": "A", "end": overdue()})
    bootstrap(conn, client, ids)
    out = run_daily_todo(conn, make_config(), client, today=TODAY, id_factory=ids).output
    assert out["run"]["claude_used"] is False
    assert out["run"]["claude_task_count"] == 0
    assert run_row(conn)["claude_used"] == 0
    assert run_row(conn)["claude_task_count"] == 0


def test_no_anthropic_import_in_todo_modules(conn):
    """docstring 문구가 아니라 실제 import 문을 검사한다."""
    import ast

    import engine.todo.claude as claude_mod
    import engine.todo.daily as daily_mod
    import engine.todo.persist as persist_mod
    import engine.todo.render as render_mod

    for module in (claude_mod, daily_mod, persist_mod, render_mod):
        tree = ast.parse(inspect.getsource(module))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert all("anthropic" not in alias.name for alias in node.names)
            if isinstance(node, ast.ImportFrom):
                assert node.module is None or "anthropic" not in node.module
    assert "anthropic" not in sys.modules


# --- 30~32: failure isolation -----------------------------
def test_persist_failure_rolls_back_daily_but_keeps_checkpoint(conn, monkeypatch):
    ids = SeqIds()
    client = client_with("m1", {"id": "W-1", "task": "A", "end": overdue()})
    bootstrap(conn, client, ids)
    client.set_modified_time("m2")
    client.set_values(rows(
        {"id": "W-1", "task": "A", "end": overdue()},
        {"id": "W-2", "task": "B", "end": overdue()},
    ))

    import engine.todo.daily as daily_mod

    monkeypatch.setattr(daily_mod, "_candidate_to_item", lambda c: {})  # KeyError inside snapshot txn

    with pytest.raises(Exception):
        run_daily_todo(conn, make_config(), client, today=TODAY, id_factory=ids)

    cp = conn.execute(
        "SELECT last_page_token FROM source_checkpoints WHERE source_type='wbs'"
    ).fetchone()
    assert cp["last_page_token"] == "m2"  # WBS sync 는 이미 커밋됨
    assert run_row(conn) is None
    assert items(conn) == []


def test_render_failure_leaves_no_daily_rows(conn, monkeypatch):
    ids = SeqIds()
    client = client_with("m1", {"id": "W-1", "task": "A", "end": overdue()})
    bootstrap(conn, client, ids)

    import engine.todo.daily as daily_mod

    def boom(**kwargs):
        raise RuntimeError("render boom")

    monkeypatch.setattr(daily_mod, "render_daily_todo", boom)
    with pytest.raises(RuntimeError):
        run_daily_todo(conn, make_config(), client, today=TODAY, id_factory=ids)
    assert run_row(conn) is None


def test_wbs_sync_failure_leaves_no_daily_rows(conn):
    ids = SeqIds()
    client = client_with("m1", {"id": "W-1", "task": "A", "end": overdue()})
    bootstrap(conn, client, ids)

    class Boom(FakeSheetsClient):
        def get_values(self, *args, **kwargs):
            raise RuntimeError("net down")

    boom = Boom()
    boom.set_modified_time("m2")
    with pytest.raises(RuntimeError):
        run_daily_todo(conn, make_config(), boom, today=TODAY, id_factory=ids)
    assert run_row(conn) is None


# --- 33~35: CLI -----------------------------------------
def test_cli_success_prints_report_without_spreadsheet_id(conn, monkeypatch, capsys):
    import scripts.wbs_daily as cli

    ids = SeqIds()
    client = client_with("m1", {"id": "W-1", "task": "지연", "end": overdue()})
    run_wbs_bootstrap(conn, make_config(), client, id_factory=ids)
    monkeypatch.setattr(cli, "load_project_config", lambda project: make_config())
    rc = cli.run_cli(
        ["--project", "demo", "--date", TODAY.isoformat()],
        client_factory=lambda: client,
        connection=conn,
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "OVERDUE" in out
    assert "SHEET1" not in out


def test_cli_client_not_configured_exit_3(conn, monkeypatch, capsys):
    import scripts.wbs_daily as cli

    monkeypatch.setattr(cli, "load_project_config", lambda project: make_config())
    rc = cli.run_cli(["--project", "demo"], connection=conn)
    assert rc == 3


def test_cli_needs_review_exit_4(conn, monkeypatch, capsys):
    import scripts.wbs_daily as cli

    ids = SeqIds()
    tasks = [{"id": f"W-{i}", "task": f"T{i:03d}", "end": overdue()} for i in range(70)]
    client = client_with("m1", *tasks)
    run_wbs_bootstrap(conn, make_config(), client, id_factory=ids)
    monkeypatch.setattr(cli, "load_project_config", lambda project: make_config())
    rc = cli.run_cli(
        ["--project", "demo", "--date", TODAY.isoformat()],
        client_factory=lambda: client,
        connection=conn,
    )
    assert rc == 4


def test_cli_skips_project_without_wbs(conn, monkeypatch, capsys):
    import scripts.wbs_daily as cli

    monkeypatch.setattr(cli, "load_project_config", lambda project: make_config(wbs=None))
    rc = cli.run_cli(["--project", "demo"], connection=conn)
    assert rc == 0
    assert "wbs 섹션이 없" in capsys.readouterr().out
