"""Daily To-Do JSON schema 1.0 renderer.

DB / 네트워크 / Google 접근 없음. TodoCandidate 목록 + run 메타 → json.dumps 가능한 dict.
"""
from __future__ import annotations

from .rules import CATEGORY_FOLLOW_UP

SCHEMA_VERSION = "1.0"


def source_ref_dict(source_ref) -> dict:
    return {
        "project_id": source_ref.project_id,
        "spreadsheet_id": source_ref.spreadsheet_id,
        "sheet_name": source_ref.sheet_name,
        "internal_task_id": source_ref.internal_task_id,
        "source_task_id": source_ref.source_task_id,
        "row_key": source_ref.row_key,
        "row_hash": source_ref.row_hash,
    }


def _todo_dict(candidate) -> dict:
    return {
        "internal_task_id": candidate.internal_task_id,
        "source_task_id": candidate.source_task_id,
        "title": candidate.task_name,
        "category": candidate.category,
        "also_categories": list(candidate.also_categories),
        "owner": candidate.owner,
        "start_date": candidate.start_date,
        "due_date": candidate.due_date,
        "progress": candidate.progress,
        "status": candidate.raw_status,
        "normalized_status": candidate.normalized_status,
        "priority": candidate.priority,
        "dependency": list(candidate.dependency),
        "reason": candidate.reason,
        "needs_confirmation": bool(candidate.needs_confirmation),
        "changed_fields": list(candidate.changed_fields),
        "source_ref": source_ref_dict(candidate.source_ref),
    }


def _counts(candidates, *, primary_only: bool) -> dict:
    counts: dict = {}
    for candidate in candidates:
        names = [candidate.category]
        if not primary_only:
            names.extend(candidate.also_categories)
        for name in names:
            counts[name] = counts.get(name, 0) + 1
    return counts


def render_daily_todo(
    *,
    project_id: str,
    project_name: str,
    today: str,
    timezone: str,
    generated_at: str,
    status: str,
    source_synced: bool,
    total_candidates: int,
    omitted_count: int,
    guardrail_hit,
    selected,
) -> dict:
    selected = list(selected)
    all_counts = _counts(selected, primary_only=False)

    return {
        "schema_version": SCHEMA_VERSION,
        "project_id": project_id,
        "project_name": project_name,
        "date": today,
        "timezone": timezone,
        "generated_at": generated_at,
        "run": {
            "status": status,
            "source_synced": bool(source_synced),
            "candidate_count": total_candidates,
            "selected_count": len(selected),
            "omitted_count": omitted_count,
            "claude_used": False,
            "claude_task_count": 0,
            "guardrail_hit": guardrail_hit,
        },
        "summary": {
            "primary_category_counts": _counts(selected, primary_only=True),
            "all_category_counts": all_counts,
            "needs_confirmation": sum(1 for c in selected if c.needs_confirmation),
            "follow_up": all_counts.get(CATEGORY_FOLLOW_UP, 0),
            "total_todos": len(selected),
        },
        "todos": [_todo_dict(c) for c in selected],
    }
