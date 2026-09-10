"""Daily To-Do orchestration.

Commit 3 WBS sync + Commit 4 candidate builder + FOLLOW_UP + persist + JSON render 를 연결한다.

원칙:
- Daily 실행은 WBS bootstrap 을 자동 실행하지 않는다 (미완료면 명확히 실패).
- today 는 config.wbs.zoneinfo() 기준 (테스트는 today 주입으로 시스템 시계 우회).
- CHANGED 판정용 old/new progress 는 sync 전/후 active state snapshot 을 비교해 구성한다
  (Commit 3 sync 인터페이스 무수정).
- WBS sync 성공 후 Daily persist 가 실패해도 WBS checkpoint 는 되돌리지 않는다.

이번 단계 범위 밖: 실제 Claude API, Slack.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
from typing import Callable, Optional

from engine.wbs.state import load_states
from engine.wbs.sync import (
    SOURCE_TYPE,
    WBSBootstrapRequired,
    WBSNotConfigured,
    generate_internal_task_id,
    run_wbs_sync,
)
from .candidates import TodoChange, build_candidates, merge_follow_up
from .claude import interpret_candidates
from .guardrails import apply_guardrails
from .render import render_daily_todo, source_ref_dict
from .persist import get_previous_unresolved_snapshot, save_daily_snapshot


class DailyPrerequisiteError(RuntimeError):
    """Daily 실행 전제(예: WBS bootstrap)가 충족되지 않음."""


@dataclasses.dataclass(frozen=True)
class DailyResult:
    output: dict           # Daily To-Do JSON schema 1.0
    run_id: Optional[int]
    sync_report: dict
    follow_up_ids: frozenset


def _today_for(config, today: Optional[dt.date]) -> dt.date:
    if today is not None:
        return today
    return dt.datetime.now(config.wbs.zoneinfo()).date()


def _build_changes(sync_report: dict, before, after) -> dict:
    """sync report['modified'] 만 TodoChange 로. 신규/재활성은 CHANGED 대상 아님."""
    changes: dict = {}
    for entry in sync_report.get("modified", []):
        internal_task_id = entry["internal_task_id"]
        old_state = before.get(internal_task_id)
        new_state = after.get(internal_task_id)
        changes[internal_task_id] = TodoChange(
            changed_fields=tuple(entry.get("changed_fields", ())),
            row_hash_changed=bool(entry.get("row_hash_changed")),
            old_progress=old_state.progress if old_state is not None else None,
            new_progress=new_state.progress if new_state is not None else None,
        )
    return changes


def _states_by_id(states) -> dict:
    return {s.internal_task_id: s for s in states}


def run_daily_todo(
    conn,
    config,
    client,
    *,
    today: Optional[dt.date] = None,
    id_factory: Callable[[], str] = generate_internal_task_id,
    claude_client=None,
    now: Optional[dt.datetime] = None,
) -> DailyResult:
    wcfg = getattr(config, "wbs", None)
    if wcfg is None:
        raise WBSNotConfigured(
            f"프로젝트 '{getattr(config, 'project_id', '?')}' 에 wbs 섹션이 없습니다."
        )

    checkpoint = conn.execute(
        "SELECT last_page_token FROM source_checkpoints WHERE project_id = ? AND source_type = ?",
        (config.project_id, SOURCE_TYPE),
    ).fetchone()
    if checkpoint is None or not checkpoint["last_page_token"]:
        raise WBSBootstrapRequired(
            f"프로젝트 '{config.project_id}' 의 WBS bootstrap 이 완료되지 않았습니다. "
            f"Daily 실행은 bootstrap 을 자동 수행하지 않습니다."
        )

    resolved_today = _today_for(config, today)

    project_id = config.project_id
    spreadsheet_id = wcfg.spreadsheet_id
    sheet_name = wcfg.sheet_name

    before_states = load_states(conn, project_id, spreadsheet_id, sheet_name, active=True)

    # --- SOURCE CHANGE (Commit 3) --- (실패 시 예외 전파 → Daily persist 안 함)
    sync_report = run_wbs_sync(conn, config, client, id_factory=id_factory)

    after_states = load_states(conn, project_id, spreadsheet_id, sheet_name, active=True)
    after_by_id = _states_by_id(after_states)

    changes = _build_changes(sync_report, _states_by_id(before_states), after_by_id)

    build_result = build_candidates(after_states, today=resolved_today, config=wcfg, changes=changes)

    previous = get_previous_unresolved_snapshot(conn, project_id, resolved_today.isoformat())
    merged, follow_up_ids = merge_follow_up(
        build_result.candidates,
        previous.items,
        after_by_id,
        wcfg,
        previous_date=previous.todo_date,
    )

    guardrail = apply_guardrails(merged)
    interpretation = interpret_candidates(guardrail.selected, client=claude_client)
    selected = interpretation.candidates

    source_synced = not sync_report.get("unchanged_source", False)
    run_status = "needs_review" if guardrail.needs_review else "success"

    generated_at = (now or dt.datetime.now(wcfg.zoneinfo())).isoformat()

    output = render_daily_todo(
        project_id=project_id,
        project_name=getattr(config, "project_name", project_id),
        today=resolved_today.isoformat(),
        timezone=wcfg.timezone,
        generated_at=generated_at,
        status=run_status,
        source_synced=source_synced,
        total_candidates=guardrail.total_candidates,
        omitted_count=guardrail.omitted_count,
        guardrail_hit=guardrail.guardrail_hit,
        selected=selected,
    )

    items = [_candidate_to_item(c) for c in selected]
    run_fields = {
        "status": run_status,
        "source_synced": source_synced,
        "candidate_count": guardrail.total_candidates,
        "claude_used": interpretation.claude_used,
        "claude_task_count": interpretation.claude_task_count,
        "guardrail_hit": guardrail.guardrail_hit,
    }

    run_id = save_daily_snapshot(
        conn,
        project_id=project_id,
        todo_date=resolved_today.isoformat(),
        run_fields=run_fields,
        items=items,
    )

    return DailyResult(
        output=output,
        run_id=run_id,
        sync_report=sync_report,
        follow_up_ids=follow_up_ids,
    )


def _candidate_to_item(candidate) -> dict:
    return {
        "internal_task_id": candidate.internal_task_id,
        "source_task_id": candidate.source_task_id,
        "row_key": candidate.row_key,
        "category": candidate.category,
        "also_categories": list(candidate.also_categories),
        "title": candidate.task_name,
        "owner": candidate.owner,
        "due_date": candidate.due_date,
        "status": candidate.raw_status,
        "priority": candidate.priority,
        "reason": candidate.reason,
        "needs_confirmation": bool(candidate.needs_confirmation),
        "source_ref": source_ref_dict(candidate.source_ref),
    }


def format_report(result: DailyResult) -> str:
    output = result.output
    run = output["run"]
    summary = output["summary"]
    lines = [
        f"Project: {output['project_name']} ({output['project_id']})",
        f"Date: {output['date']} ({output['timezone']})",
        f"Source synced: {run['source_synced']}",
        f"Status: {run['status']}",
        f"Total candidates: {run['candidate_count']}",
        f"Selected: {run['selected_count']}",
        f"Omitted: {run['omitted_count']}",
    ]
    for category, count in summary["primary_category_counts"].items():
        lines.append(f"{category}: {count}")
    lines.append(f"FOLLOW_UP (incl. secondary): {summary['follow_up']}")
    lines.append(f"Needs confirmation: {summary['needs_confirmation']}")
    lines.append("")
    for todo in output["todos"]:
        owner = todo["owner"] or "-"
        due = todo["due_date"] or "-"
        lines.append(f"[{todo['category']}] {todo['title']} — {owner} — {due} — {todo['reason']}")
    return "\n".join(lines)
