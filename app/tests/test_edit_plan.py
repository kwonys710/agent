from __future__ import annotations

from pathlib import Path

import pytest

from dailyreels.edit_plan import (
    EditPlanError,
    build_render_plan,
    center_start,
    clamp_use_duration,
    load_edit_plan,
)


@pytest.fixture
def root(tmp_path: Path) -> Path:
    for name in ("clip_001.mp4", "clip_002.mp4", "clip_003.mp4"):
        (tmp_path / name).write_bytes(b"x")
    return tmp_path


def plan_dict(**overrides) -> dict:
    plan = {
        "date": "2026-09-12",
        "story": "식사와 도서관 방문으로 채운 하루",
        "target_duration": 42,
        "hook": {"file": "clip_003.mp4", "caption": "철판에 골고루\n볶는 밥.", "duration": 2.0,
                 "source_duration": 10.0, "id": "clip_003"},
        "clips": [
            {"id": "clip_001", "file": "clip_001.mp4", "time": "07:50",
             "caption": "이른 아침 이동 중.", "duration": 2.0, "source_duration": 10.0},
            {"id": "clip_002", "file": "clip_002.mp4", "time": "09:17",
             "caption": "도착.", "duration": 1.5, "source_duration": 8.0},
        ],
    }
    plan.update(overrides)
    return plan


def test_center_start_uses_the_middle_of_the_source():
    assert center_start(10.0, 2.0) == 4.0
    assert center_start(2.0, 2.0) == 0.0
    assert center_start(1.0, 2.0) == 0.0  # never negative


def test_clamp_use_duration_never_exceeds_source():
    assert clamp_use_duration(10.0, 2.0) == 2.0
    assert clamp_use_duration(1.2, 3.0) == 1.2
    assert clamp_use_duration(0.0, 3.0) == 0.0
    assert clamp_use_duration(10.0, 0.0) == pytest.approx(0.3)


def test_build_render_plan_puts_hook_first_and_center_trims(root):
    plan = build_render_plan(plan_dict(), root=root)

    assert [clip.clip_id for clip in plan.clips] == ["clip_003", "clip_001", "clip_002"]
    assert plan.hook is not None and plan.hook.is_hook
    assert plan.hook.caption == "철판에 골고루\n볶는 밥."
    assert plan.clips[1].start == 4.0          # (10 - 2) / 2
    assert plan.clips[2].start == pytest.approx(3.25)  # (8 - 1.5) / 2
    assert plan.total_duration == pytest.approx(5.5)
    assert plan.date == "2026-09-12"


def test_hook_duplicate_in_body_is_dropped(root):
    raw = plan_dict()
    raw["clips"].append(
        {"id": "clip_003", "file": "clip_003.mp4", "time": "13:03", "caption": "밥.",
         "duration": 2.0, "source_duration": 10.0}
    )
    plan = build_render_plan(raw, root=root)

    assert [clip.clip_id for clip in plan.clips] == ["clip_003", "clip_001", "clip_002"]
    assert plan.dropped_duplicates == ["clip_003"]
    assert len(plan.body) == 2


def test_hook_given_as_plain_text_styles_the_first_clip(root):
    raw = plan_dict(hook="철판에 골고루 볶는 밥.")
    plan = build_render_plan(raw, root=root)

    assert plan.hook is not None
    assert plan.hook.clip_id == "clip_001"
    assert plan.hook.caption == "철판에 골고루 볶는 밥."
    assert len(plan.clips) == 2


def test_use_duration_is_clamped_to_a_short_source(root):
    raw = plan_dict(hook=None)
    raw["clips"][0]["source_duration"] = 1.2
    raw["clips"][0]["duration"] = 3.0
    plan = build_render_plan(raw, root=root)

    clip = plan.clips[0]
    assert clip.use_duration == 1.2
    assert clip.start == 0.0
    assert clip.end <= clip.source_duration


def test_missing_source_duration_is_probed_not_guessed(root):
    raw = plan_dict(hook=None)
    del raw["clips"][0]["source_duration"]
    calls: list[Path] = []

    def duration_of(path: Path) -> float:
        calls.append(path)
        return 9.0

    plan = build_render_plan(raw, root=root, duration_of=duration_of)

    assert [path.name for path in calls] == ["clip_001.mp4"]
    assert plan.clips[0].start == 3.5  # (9 - 2) / 2


def test_trim_mode_plan_honours_an_explicit_start(root):
    raw = plan_dict(hook=None)
    raw["clips"][0]["start"] = 6.5
    plan = build_render_plan(raw, root=root, trim_mode="plan")
    assert plan.clips[0].start == 6.5

    centered = build_render_plan(raw, root=root, trim_mode="center")
    assert centered.clips[0].start == 4.0


def test_missing_source_file_is_a_clear_error(root):
    raw = plan_dict(hook=None)
    raw["clips"][0]["file"] = "gone.mp4"
    with pytest.raises(EditPlanError, match="source video missing"):
        build_render_plan(raw, root=root)


def test_empty_plan_rejected(root):
    with pytest.raises(EditPlanError, match="no clips"):
        build_render_plan({"clips": []}, root=root)


def test_load_edit_plan_reports_missing_file(tmp_path):
    with pytest.raises(EditPlanError, match="edit_plan not found"):
        load_edit_plan(tmp_path / "edit_plan.json")


def test_load_edit_plan_reads_utf8_with_bom(tmp_path):
    path = tmp_path / "edit_plan.json"
    path.write_text('{"story": "도서관"}', encoding="utf-8-sig")
    assert load_edit_plan(path)["story"] == "도서관"
