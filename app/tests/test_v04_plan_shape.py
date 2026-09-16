"""The shape v0.4 actually emits: hook as {clip_id, caption, duration} with the file in analysis.json."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from dailyreels.edit_plan import EditPlanError, build_render_plan
from dailyreels.renderer import load_clip_index, render

CLIPS = [
    ("clip_001", "20260912_075033.mp4", "07:50", "이른 아침 이동 중.", 8.959854, 2.0),
    ("clip_002", "20260912_091741.mp4", "09:17", "오전에 챙겨 먹는 식사.", 11.7972, 2.0),
    ("clip_003", "20260912_130324.mp4", "13:03", "점심 식당 앞 도착.", 8.9013, 2.0),
    ("clip_005", "20260912_131343.mp4", "13:14", "푸짐하게 끓인 전골.", 15.274438, 2.0),
    ("clip_006", "20260912_132222.mp4", "13:22", "고기 한 점 맛보기.", 6.335896, 1.8),
    ("clip_008", "20260912_134824.mp4", "13:48", "깻잎에 얹어 한 입.", 4.8322, 2.0),
    ("clip_009", "20260912_142302.mp4", "14:23", "오후 도서관 들어서는 길.", 11.8294, 2.0),
    ("clip_011", "20260912_152253.mp4", "15:23", "창가에서 책과 커피 한잔.", 9.303856, 2.5),
]
HOOK_ID, HOOK_FILE, HOOK_DURATION = "clip_007", "20260912_134034.mp4", 2.5


def write_plan(root: Path) -> Path:
    plan = {
        "schema_version": "0.4",
        "date": "2026-09-12",
        "story": "식사와 도서관 방문으로 채운 하루",
        "hook": {"clip_id": HOOK_ID, "caption": "철판에 골고루 볶는 밥.", "duration": HOOK_DURATION},
        "target_duration": 18.8,
        "clips": [
            {"clip_id": cid, "file": name, "time_label": label, "scene": "meal",
             "source_duration": source, "use_duration": use, "caption": caption,
             "highlight_score": 5.0, "order": index + 1}
            for index, (cid, name, label, caption, source, use) in enumerate(CLIPS)
        ],
    }
    path = root / "data" / "edit_plan.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
    return path


def write_analysis(root: Path) -> Path:
    payload = {
        "schema_version": "0.3",
        "clips": [{"clip_id": cid, "file": name, "analysis": {"scene": "meal"}}
                  for cid, name, *_ in CLIPS]
        + [{"clip_id": HOOK_ID, "file": HOOK_FILE, "analysis": {"scene": "meal"}}],
        "errors": [],
    }
    path = root / "data" / "analysis.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


@pytest.fixture
def plan_root(tmp_path: Path) -> Path:
    for _, name, *_ in CLIPS:
        (tmp_path / name).write_bytes(b"x")
    (tmp_path / HOOK_FILE).write_bytes(b"x")
    write_plan(tmp_path)
    write_analysis(tmp_path)
    return tmp_path


def test_load_clip_index_reads_analysis_json(plan_root):
    index = load_clip_index(plan_root / "data")
    assert index[HOOK_ID] == HOOK_FILE
    assert index["clip_001"] == "20260912_075033.mp4"


def test_hook_clip_id_outside_the_timeline_is_resolved(plan_root):
    raw = json.loads((plan_root / "data" / "edit_plan.json").read_text(encoding="utf-8"))
    plan = build_render_plan(
        raw, root=plan_root, clip_files=load_clip_index(plan_root / "data"),
        duration_of=lambda path: 74.0,
    )

    hook = plan.hook
    assert hook is not None
    assert (hook.clip_id, hook.file) == (HOOK_ID, HOOK_FILE)
    assert hook.caption == "철판에 골고루 볶는 밥."
    assert hook.use_duration == HOOK_DURATION
    assert hook.start == pytest.approx((74.0 - 2.5) / 2)  # centre of the source
    assert plan.dropped_duplicates == []


def test_plan_use_durations_are_used_verbatim(plan_root):
    raw = json.loads((plan_root / "data" / "edit_plan.json").read_text(encoding="utf-8"))
    plan = build_render_plan(
        raw, root=plan_root, clip_files=load_clip_index(plan_root / "data"),
        duration_of=lambda path: 74.0,
    )

    assert [clip.clip_id for clip in plan.body] == [cid for cid, *_ in CLIPS]
    assert [clip.use_duration for clip in plan.body] == [use for *_, use in CLIPS]
    assert plan.total_duration == pytest.approx(18.8)      # == target_duration
    assert plan.body[0].start == pytest.approx((8.959854 - 2.0) / 2)
    assert plan.body[4].start == pytest.approx((6.335896 - 1.8) / 2)


def test_unresolvable_hook_clip_id_fails_loudly(plan_root):
    raw = json.loads((plan_root / "data" / "edit_plan.json").read_text(encoding="utf-8"))
    with pytest.raises(EditPlanError, match="clip_007 has no source file"):
        build_render_plan(raw, root=plan_root, clip_files={})  # no analysis.json


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg not installed")
def test_real_plan_shape_renders(make_settings, tmp_path):
    root = tmp_path
    sources = [(name, source) for _, name, _, _, source, _ in CLIPS] + [(HOOK_FILE, 74.0)]
    for name, seconds in sources:
        subprocess.run(
            ["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
             "-i", f"testsrc2=s=1080x1920:r=30:d={seconds:.3f}",
             "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", str(root / name)],
            check=True, capture_output=True,
        )
    write_plan(root)
    write_analysis(root)

    result = render(make_settings(root=root), previews=False)

    assert (result.width, result.height) == (1080, 1920)
    assert result.duration == pytest.approx(18.8, abs=0.75)
    assert result.plan.hook is not None and result.plan.hook.file == HOOK_FILE
    assert len(result.plan.body) == 8
