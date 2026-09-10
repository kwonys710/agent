"""Daily To-Do deterministic rule engine 테스트 (Commit 4).

pure unit test — FakeSheets/DB/네트워크 불필요. today 는 항상 주입.
"""
import datetime as dt
import inspect

import pytest

from engine.todo.candidates import (
    SourceRef,
    TodoCandidate,
    TodoChange,
    build_candidates,
    sort_candidates,
)
from engine.todo.guardrails import MAX_TODO_CANDIDATES, apply_guardrails
from engine.todo.rules import (
    CATEGORY_BLOCKED,
    CATEGORY_CHANGED,
    CATEGORY_DUE_SOON,
    CATEGORY_DUE_TODAY,
    CATEGORY_NEEDS_CONFIRMATION,
    CATEGORY_OVERDUE,
    CATEGORY_START_TODAY,
    evaluate_task,
    is_done,
)
from engine.wbs.config import WBSConfig
from engine.wbs.state import TaskState

TODAY = dt.date(2026, 9, 10)


def iso(d):
    return d.isoformat()


def days(n):
    return TODAY + dt.timedelta(days=n)


def make_config(**overrides):
    raw = {
        "spreadsheet_id": "SHEET1",
        "sheet_name": "WBS",
        "due_soon_days": 3,
        "progress_done_threshold": 1.0,
        "progress_change_min_delta": 0.1,
        "columns": {"task_name": "Task"},
        "status_map": {"DONE": ["완료"], "BLOCKED": ["지연"], "IN_PROGRESS": ["진행"]},
        "blocked_keywords": ["블로커", "차단"],
        "change_significant_fields": ["start_date", "end_date", "owner", "normalized_status", "progress"],
    }
    raw.update(overrides)
    return WBSConfig.from_raw(raw)


_TS_DEFAULTS = dict(
    internal_task_id="i1",
    project_id="demo",
    spreadsheet_id="SHEET1",
    sheet_name="WBS",
    source_task_id="W-1",
    source_identity_key="k1",
    row_key="WBS!R2",
    task_name="작업",
    project=None,
    phase=None,
    owner="김",
    start_date=None,
    end_date=None,
    progress=None,
    raw_status="진행",
    normalized_status="IN_PROGRESS",
    priority=None,
    dependency=[],
    notes=None,
    source_updated_at=None,
    parse_errors=[],
    row_hash="h0",
    first_seen_at="t",
    last_seen_at="t",
    last_changed_at="t",
    is_active=True,
)


def task_state(**overrides):
    data = dict(_TS_DEFAULTS)
    data.update(overrides)
    return TaskState(**data)


def categories(hits):
    return [h.category for h in hits]


# --- 1~2: OVERDUE --------------------------------------------------------
def test_overdue():
    t = task_state(end_date=iso(days(-2)))
    assert categories(evaluate_task(t, today=TODAY, config=make_config())) == [CATEGORY_OVERDUE]


def test_overdue_days_calculation():
    t = task_state(end_date=iso(days(-5)))
    hit = evaluate_task(t, today=TODAY, config=make_config())[0]
    assert hit.detail["days_overdue"] == 5
    assert "5일" in hit.reason


# --- 3~6: DUE_TODAY / DUE_SOON ---------------------------------------
def test_due_today():
    t = task_state(end_date=iso(TODAY))
    assert categories(evaluate_task(t, today=TODAY, config=make_config())) == [CATEGORY_DUE_TODAY]


def test_due_soon_plus_one():
    t = task_state(end_date=iso(days(1)))
    hits = evaluate_task(t, today=TODAY, config=make_config())
    assert categories(hits) == [CATEGORY_DUE_SOON]
    assert hits[0].detail["days_remaining"] == 1


def test_due_soon_boundary_equals_due_soon_days():
    t = task_state(end_date=iso(days(3)))
    assert categories(evaluate_task(t, today=TODAY, config=make_config())) == [CATEGORY_DUE_SOON]


def test_due_soon_out_of_range():
    t = task_state(end_date=iso(days(4)))
    assert evaluate_task(t, today=TODAY, config=make_config()) == []


# --- 7: START_TODAY -------------------------------------------------
def test_start_today():
    t = task_state(start_date=iso(TODAY), end_date=iso(days(30)))
    assert categories(evaluate_task(t, today=TODAY, config=make_config())) == [CATEGORY_START_TODAY]


# --- 8~10: BLOCKED ------------------------------------------------
def test_blocked_by_status():
    t = task_state(normalized_status="BLOCKED")
    hit = evaluate_task(t, today=TODAY, config=make_config())[0]
    assert hit.category == CATEGORY_BLOCKED
    assert hit.detail["source"] == "status"


def test_blocked_by_notes_keyword():
    t = task_state(notes="외부 승인 대기로 차단 상태")
    hit = evaluate_task(t, today=TODAY, config=make_config())[0]
    assert hit.category == CATEGORY_BLOCKED
    assert hit.detail["keyword"] == "차단"
    assert "차단" in hit.reason


def test_blocked_keyword_no_match():
    t = task_state(notes="정상 진행중")
    assert evaluate_task(t, today=TODAY, config=make_config()) == []


def test_blocked_scan_disabled_when_keywords_empty():
    t = task_state(notes="차단됨")
    assert evaluate_task(t, today=TODAY, config=make_config(blocked_keywords=[])) == []


# --- 11~13: 제외 ------------------------------------------------
def test_done_status_excluded_from_all_categories():
    t = task_state(normalized_status="DONE", end_date=iso(days(-5)))
    assert evaluate_task(t, today=TODAY, config=make_config()) == []
    assert is_done(t, make_config()) is True
    res = build_candidates([t], today=TODAY, config=make_config())
    assert res.candidates == ()
    assert res.excluded_done == ("i1",)


def test_progress_threshold_excludes_task():
    done = task_state(progress=1.0, end_date=iso(days(-5)))
    res = build_candidates([done], today=TODAY, config=make_config())
    assert res.excluded_done == ("i1",)

    almost = task_state(progress=0.99, end_date=iso(days(-5)))
    res2 = build_candidates([almost], today=TODAY, config=make_config())
    assert res2.candidates[0].category == CATEGORY_OVERDUE


def test_inactive_task_excluded():
    t = task_state(is_active=False, end_date=iso(days(-5)))
    res = build_candidates([t], today=TODAY, config=make_config())
    assert res.candidates == ()
    assert res.excluded_inactive == ("i1",)


# --- 14~16: NEEDS_CONFIRMATION -------------------------------
def test_unknown_status_needs_confirmation():
    t = task_state(normalized_status="UNKNOWN", raw_status="검토중")
    hits = evaluate_task(t, today=TODAY, config=make_config())
    assert categories(hits) == [CATEGORY_NEEDS_CONFIRMATION]
    assert "검토중" in hits[0].reason


def test_parse_errors_needs_confirmation():
    t = task_state(parse_errors=["invalid_start_date"])
    hits = evaluate_task(t, today=TODAY, config=make_config())
    assert categories(hits) == [CATEGORY_NEEDS_CONFIRMATION]
    assert "invalid_start_date" in hits[0].reason


def test_unknown_and_overdue_primary_overdue_also_needs_confirmation():
    t = task_state(normalized_status="UNKNOWN", raw_status="?", end_date=iso(days(-1)))
    cand = build_candidates([t], today=TODAY, config=make_config()).candidates[0]
    assert cand.category == CATEGORY_OVERDUE
    assert CATEGORY_NEEDS_CONFIRMATION in cand.also_categories
    assert cand.needs_confirmation is True


# --- done + confirmation override -------------------------------
def test_done_by_progress_but_unknown_status_still_surfaces_as_needs_confirmation():
    t = task_state(normalized_status="UNKNOWN", raw_status="?", progress=1.0,
                   end_date=iso(days(-3)), parse_errors=[])
    res = build_candidates([t], today=TODAY, config=make_config())
    assert len(res.candidates) == 1
    cand = res.candidates[0]
    assert cand.category == CATEGORY_NEEDS_CONFIRMATION
    assert cand.also_categories == ()
    assert CATEGORY_OVERDUE not in (cand.category, *cand.also_categories)
    assert "i1" not in res.excluded_done
    assert cand.needs_confirmation is True


def test_done_status_with_parse_errors_still_surfaces_as_needs_confirmation():
    t = task_state(normalized_status="DONE", parse_errors=["invalid_end_date"], end_date=iso(days(1)))
    res = build_candidates([t], today=TODAY, config=make_config())
    assert [c.category for c in res.candidates] == [CATEGORY_NEEDS_CONFIRMATION]
    assert res.candidates[0].also_categories == ()
    assert res.excluded_done == ()
    assert "invalid_end_date" in res.candidates[0].reason


def test_done_status_without_confirmation_issue_stays_excluded():
    t = task_state(normalized_status="DONE", parse_errors=[], end_date=iso(days(-5)))
    res = build_candidates([t], today=TODAY, config=make_config())
    assert res.candidates == ()
    assert res.excluded_done == ("i1",)


def test_done_by_progress_without_confirmation_issue_stays_excluded():
    t = task_state(progress=1.0, normalized_status="IN_PROGRESS", parse_errors=[], end_date=iso(days(-5)))
    res = build_candidates([t], today=TODAY, config=make_config())
    assert res.candidates == ()
    assert res.excluded_done == ("i1",)


def test_confirmation_override_reason_states_done_fact_only():
    t = task_state(normalized_status="UNKNOWN", raw_status="보류?", progress=1.0)
    reason = build_candidates([t], today=TODAY, config=make_config()).candidates[0].reason
    assert "확인이 필요함" in reason
    for banned in ("중요", "긴급", "반드시"):
        assert banned not in reason


# --- 17~18: primary category priority ---------------------------
def test_overdue_and_blocked_priority():
    t = task_state(normalized_status="BLOCKED", end_date=iso(days(-2)))
    cand = build_candidates([t], today=TODAY, config=make_config()).candidates[0]
    assert cand.category == CATEGORY_OVERDUE
    assert cand.also_categories == (CATEGORY_BLOCKED,)
    assert "지남" in cand.reason and "BLOCKED" in cand.reason


def test_due_today_and_start_today_priority():
    t = task_state(start_date=iso(TODAY), end_date=iso(TODAY))
    cand = build_candidates([t], today=TODAY, config=make_config()).candidates[0]
    assert cand.category == CATEGORY_DUE_TODAY
    assert cand.also_categories == (CATEGORY_START_TODAY,)


# --- 19~20: dedupe -------------------------------------------
def test_one_candidate_per_internal_task_id():
    t = task_state(start_date=iso(TODAY), end_date=iso(TODAY), normalized_status="BLOCKED")
    res = build_candidates([t], today=TODAY, config=make_config())
    assert len(res.candidates) == 1
    cand = res.candidates[0]
    assert cand.category == CATEGORY_BLOCKED  # BLOCKED(1) < DUE_TODAY(2)
    assert set(cand.also_categories) == {CATEGORY_DUE_TODAY, CATEGORY_START_TODAY}


def test_duplicate_source_task_id_kept_as_two_candidates():
    a = task_state(internal_task_id="i1", source_task_id="W-1", row_key="WBS!R2", end_date=iso(days(-1)))
    b = task_state(internal_task_id="i2", source_task_id="W-1", row_key="WBS!R3", end_date=iso(days(-1)))
    res = build_candidates([a, b], today=TODAY, config=make_config())
    assert {c.internal_task_id for c in res.candidates} == {"i1", "i2"}


# --- 21~28: CHANGED --------------------------------------------
def _future_task():
    return task_state(end_date=iso(days(30)))


def test_changed_significant_end_date():
    hits = evaluate_task(_future_task(), today=TODAY, config=make_config(),
                         change=TodoChange(changed_fields=("end_date",), row_hash_changed=True))
    assert CATEGORY_CHANGED in categories(hits)


def test_changed_notes_only_not_significant():
    hits = evaluate_task(_future_task(), today=TODAY, config=make_config(),
                         change=TodoChange(changed_fields=("notes",), row_hash_changed=False))
    assert hits == []


def test_changed_owner_reason_uses_field_label():
    hits = evaluate_task(_future_task(), today=TODAY, config=make_config(),
                         change=TodoChange(changed_fields=("owner",)))
    assert categories(hits) == [CATEGORY_CHANGED]
    assert "담당자" in hits[0].reason


def test_changed_normalized_status():
    hits = evaluate_task(_future_task(), today=TODAY, config=make_config(),
                         change=TodoChange(changed_fields=("normalized_status",)))
    assert categories(hits) == [CATEGORY_CHANGED]


def test_changed_progress_below_delta_not_significant():
    hits = evaluate_task(_future_task(), today=TODAY, config=make_config(),
                         change=TodoChange(changed_fields=("progress",), old_progress=0.50, new_progress=0.55))
    assert CATEGORY_CHANGED not in categories(hits)


def test_changed_progress_above_delta_significant():
    hits = evaluate_task(_future_task(), today=TODAY, config=make_config(),
                         change=TodoChange(changed_fields=("progress",), old_progress=0.50, new_progress=0.80))
    assert categories(hits) == [CATEGORY_CHANGED]


def test_changed_progress_without_old_new_stays_significant():
    hits = evaluate_task(_future_task(), today=TODAY, config=make_config(),
                         change=TodoChange(changed_fields=("progress",)))
    assert categories(hits) == [CATEGORY_CHANGED]


def test_changed_progress_below_delta_but_other_field_significant():
    hits = evaluate_task(_future_task(), today=TODAY, config=make_config(),
                         change=TodoChange(changed_fields=("progress", "owner"), old_progress=0.5, new_progress=0.52))
    assert categories(hits) == [CATEGORY_CHANGED]
    assert "담당자" in hits[0].reason and "진척률" not in hits[0].reason


def test_changed_done_task_excluded():
    t = task_state(normalized_status="DONE", end_date=iso(days(1)))
    res = build_candidates([t], today=TODAY, config=make_config(),
                           changes={"i1": TodoChange(changed_fields=("normalized_status",))})
    assert res.candidates == ()
    assert res.excluded_done == ("i1",)


def test_row_hash_changed_without_significant_field_is_not_changed():
    hits = evaluate_task(_future_task(), today=TODAY, config=make_config(),
                         change=TodoChange(changed_fields=("notes", "phase"), row_hash_changed=True))
    assert hits == []


def test_changed_is_independent_of_row_hash_changed_flag():
    cfg = make_config(change_significant_fields=["notes"])
    hits = evaluate_task(_future_task(), today=TODAY, config=cfg,
                         change=TodoChange(changed_fields=("notes",), row_hash_changed=False))
    assert categories(hits) == [CATEGORY_CHANGED]


# --- 29~30: reason / source_ref ------------------------------
def test_deterministic_reason_is_factual_and_not_speculative():
    t = task_state(normalized_status="BLOCKED", end_date=iso(dt.date(2026, 9, 8)))
    reason = build_candidates([t], today=TODAY, config=make_config()).candidates[0].reason
    assert reason == "종료일 2026-09-08이(가) 오늘(2026-09-10)보다 2일 지남. 정규화 상태가 BLOCKED임."
    for banned in ("중요", "긴급", "반드시", "시급"):
        assert banned not in reason


def test_source_ref_preserved_on_every_candidate():
    t = task_state(end_date=iso(days(-1)), row_hash="abc123")
    sr = build_candidates([t], today=TODAY, config=make_config()).candidates[0].source_ref
    assert (sr.project_id, sr.spreadsheet_id, sr.sheet_name) == ("demo", "SHEET1", "WBS")
    assert (sr.internal_task_id, sr.source_task_id, sr.row_key, sr.row_hash) == (
        "i1", "W-1", "WBS!R2", "abc123",
    )


# --- 31~32: union / also_categories order --------------------
def test_multiple_categories_union_and_also_ordering():
    t = task_state(
        start_date=iso(TODAY),
        end_date=iso(days(-1)),
        normalized_status="BLOCKED",
        parse_errors=["invalid_progress"],
    )
    cand = build_candidates([t], today=TODAY, config=make_config()).candidates[0]
    assert cand.category == CATEGORY_OVERDUE
    assert cand.also_categories == (CATEGORY_BLOCKED, CATEGORY_START_TODAY, CATEGORY_NEEDS_CONFIRMATION)
    assert cand.needs_confirmation is True


def test_category_counts_and_needs_confirmation_count():
    tasks = [
        task_state(internal_task_id="a", source_task_id="A", end_date=iso(days(-1))),
        task_state(internal_task_id="b", source_task_id="B", end_date=iso(days(-1))),
        task_state(internal_task_id="c", source_task_id="C", end_date=iso(TODAY), parse_errors=["invalid_end_date"]),
    ]
    res = build_candidates(tasks, today=TODAY, config=make_config())
    assert res.category_counts == {CATEGORY_OVERDUE: 2, CATEGORY_DUE_TODAY: 1}
    assert res.needs_confirmation_count == 1


# --- 33~36: guardrail -------------------------------------
def _cand(iid, category, due=None):
    return TodoCandidate(
        internal_task_id=iid, source_task_id=None, row_key=f"WBS!R{iid}",
        task_name=iid, owner=None, start_date=None, due_date=due, progress=None,
        raw_status=None, normalized_status="IN_PROGRESS", priority=None,
        dependency=(), notes=None, category=category, also_categories=(),
        reason="r", needs_confirmation=False, changed_fields=(), row_hash_changed=False,
        source_ref=SourceRef("demo", "S", "WBS", iid, None, f"WBS!R{iid}", "h"),
    )


def _many(n, category=CATEGORY_OVERDUE):
    return [_cand(f"i{i:03d}", category, due=f"2026-09-{(i % 27) + 1:02d}") for i in range(n)]


def test_guardrail_under_limit_passes_through():
    r = apply_guardrails(_many(10))
    assert r.ok and not r.needs_review
    assert r.guardrail_hit is None
    assert len(r.selected) == 10
    assert r.omitted_count == 0


def test_guardrail_over_limit_selects_and_flags():
    r = apply_guardrails(_many(75))
    assert not r.ok and r.needs_review
    assert r.guardrail_hit == "MAX_TODO_CANDIDATES"
    assert r.total_candidates == 75
    assert len(r.selected) == MAX_TODO_CANDIDATES
    assert r.omitted_count == 15


def test_guardrail_selection_is_priority_first_and_order_independent():
    overdue = [_cand(f"o{i}", CATEGORY_OVERDUE, due=f"2026-09-{i + 1:02d}") for i in range(9)]
    soon = [_cand(f"s{i}", CATEGORY_DUE_SOON, due="2026-09-20") for i in range(99)]
    r1 = apply_guardrails(soon + overdue, max_todo_candidates=5)
    r2 = apply_guardrails(overdue + soon, max_todo_candidates=5)
    assert [c.category for c in r1.selected] == [CATEGORY_OVERDUE] * 5
    assert r1.selected == r2.selected


def test_sort_candidates_orders_by_priority_then_due_then_name_then_id():
    a = _cand("z", CATEGORY_DUE_SOON, due="2026-09-15")
    b = _cand("a", CATEGORY_OVERDUE, due=None)
    c = _cand("b", CATEGORY_OVERDUE, due="2026-09-01")
    order = [x.internal_task_id for x in sort_candidates([a, b, c])]
    assert order == ["b", "a", "z"]  # OVERDUE(due set) < OVERDUE(due None) < DUE_SOON


# --- 37~38: determinism / no-now ---------------------------
def test_today_is_injected_not_system_clock():
    t = task_state(end_date=iso(dt.date(2000, 1, 1)))
    assert categories(evaluate_task(t, today=dt.date(2000, 1, 2), config=make_config())) == [CATEGORY_OVERDUE]
    assert evaluate_task(t, today=dt.date(1999, 12, 25), config=make_config()) == []


def test_rule_modules_do_not_read_system_clock():
    """docstring 이 아니라 실제 call 노드를 검사한다 (today 는 반드시 주입)."""
    import ast

    import engine.todo.candidates as cand_mod
    import engine.todo.guardrails as guard_mod
    import engine.todo.rules as rules_mod

    forbidden = {"now", "today", "utcnow", "time", "monotonic", "perf_counter"}
    for module in (rules_mod, cand_mod, guard_mod):
        tree = ast.parse(inspect.getsource(module))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = getattr(func, "attr", None) or getattr(func, "id", None)
            assert name not in forbidden, f"{module.__name__} calls {name}()"
