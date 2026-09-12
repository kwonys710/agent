from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date as date_cls
from pathlib import Path
from typing import Any, Callable, Iterable

log = logging.getLogger(__name__)

MIN_CLIP_DURATION = 0.3

_FILE_KEYS = ("file", "filename", "source", "source_file", "video", "path")
_ID_KEYS = ("id", "clip_id", "clip")
_USE_KEYS = ("use_duration", "duration", "clip_duration", "length")
_SOURCE_DURATION_KEYS = ("source_duration", "original_duration", "full_duration")
_CAPTION_KEYS = ("caption", "text", "subtitle", "line")
_TIME_KEYS = ("time_label", "time", "timestamp", "captured_time")
_START_KEYS = ("start", "source_start", "trim_start", "offset")
_SCENE_KEYS = ("scene", "scene_type", "category")
_CLIPS_KEYS = ("clips", "timeline", "body", "segments")


class EditPlanError(RuntimeError):
    """edit_plan.json is missing, malformed, or points at files that aren't there."""


@dataclass(frozen=True)
class RenderClip:
    clip_id: str
    file: str
    path: Path
    source_duration: float
    use_duration: float
    start: float
    time_label: str
    caption: str
    scene: str
    is_hook: bool = False

    @property
    def end(self) -> float:
        return self.start + self.use_duration


@dataclass
class RenderPlan:
    date: str
    story: str
    target_duration: float
    clips: list[RenderClip] = field(default_factory=list)
    dropped_duplicates: list[str] = field(default_factory=list)

    @property
    def hook(self) -> RenderClip | None:
        return self.clips[0] if self.clips and self.clips[0].is_hook else None

    @property
    def body(self) -> list[RenderClip]:
        return [clip for clip in self.clips if not clip.is_hook]

    @property
    def total_duration(self) -> float:
        return sum(clip.use_duration for clip in self.clips)


def _first(data: dict[str, Any], keys: Iterable[str]) -> Any:
    for key in keys:
        value = data.get(key)
        if value not in (None, ""):
            return value
    return None


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def center_start(source_duration: float, use_duration: float) -> float:
    """Deterministic trim point: the middle of the source clip (v0.5 rule)."""
    return max(0.0, (source_duration - use_duration) / 2.0)


def clamp_use_duration(source_duration: float, requested: float) -> float:
    """Never ask ffmpeg for more footage than the file has."""
    if source_duration <= 0:
        return 0.0
    if requested <= 0:
        return min(source_duration, MIN_CLIP_DURATION)
    return min(requested, source_duration)


def load_edit_plan(path: Path) -> dict[str, Any]:
    import json

    if not path.is_file():
        raise EditPlanError(
            f"edit_plan not found: {path}\nRun the planner (v0.4) first."
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EditPlanError(f"could not read {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise EditPlanError(f"{path} must contain a JSON object")
    return payload


def _raw_clips(plan: dict[str, Any]) -> list[dict[str, Any]]:
    raw = _first(plan, _CLIPS_KEYS)
    if isinstance(raw, dict):  # {"clips": {"clip_001": {...}}}
        raw = [dict(value, id=key) for key, value in raw.items()]
    if not isinstance(raw, list) or not raw:
        raise EditPlanError("edit_plan has no clips")
    return [clip for clip in raw if isinstance(clip, dict)]


def _build_clip(
    raw: dict[str, Any],
    root: Path,
    trim_mode: str,
    duration_of: Callable[[Path], float] | None,
    is_hook: bool,
    index: int,
) -> RenderClip:
    filename = _first(raw, _FILE_KEYS)
    if not filename:
        raise EditPlanError(f"clip #{index + 1} has no source file field")

    path = Path(str(filename))
    if not path.is_absolute():
        path = root / path
    if not path.is_file():
        raise EditPlanError(f"source video missing: {path}")

    source_duration = _as_float(_first(raw, _SOURCE_DURATION_KEYS))
    if source_duration <= 0 and duration_of is not None:
        source_duration = duration_of(path)
    if source_duration <= 0:
        raise EditPlanError(f"could not determine source duration for {path.name}")

    use_duration = clamp_use_duration(source_duration, _as_float(_first(raw, _USE_KEYS)))
    if use_duration <= 0:
        raise EditPlanError(f"{path.name}: usable duration is zero")

    explicit_start = _first(raw, _START_KEYS)
    if trim_mode == "plan" and explicit_start is not None:
        start = min(max(0.0, _as_float(explicit_start)), max(0.0, source_duration - use_duration))
    else:
        start = center_start(source_duration, use_duration)

    return RenderClip(
        clip_id=str(_first(raw, _ID_KEYS) or f"clip_{index + 1:03d}"),
        file=path.name,
        path=path,
        source_duration=source_duration,
        use_duration=use_duration,
        start=start,
        time_label=str(_first(raw, _TIME_KEYS) or ""),
        caption=str(_first(raw, _CAPTION_KEYS) or ""),
        scene=str(_first(raw, _SCENE_KEYS) or ""),
        is_hook=is_hook,
    )


def _hook_raw(plan: dict[str, Any], clips: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, str]:
    """Return the hook's raw dict plus the hook text, from any of the shapes v0.4 emits."""
    hook = plan.get("hook")
    if isinstance(hook, dict):
        text = str(_first(hook, _CAPTION_KEYS) or "")
        if _first(hook, _FILE_KEYS):
            return hook, text
        # hook carries only text / a clip reference
        ref = str(_first(hook, _ID_KEYS) or "")
        for raw in clips:
            if ref and str(_first(raw, _ID_KEYS) or "") == ref:
                merged = dict(raw)
                if text:
                    merged["caption"] = text
                return merged, text
        return None, text

    for raw in clips:  # clip flagged inside the timeline
        if raw.get("is_hook") or raw.get("hook") is True:
            return raw, str(_first(raw, _CAPTION_KEYS) or "")

    if isinstance(hook, str) and hook.strip():
        return None, hook.strip()
    return None, ""


def build_render_plan(
    plan: dict[str, Any],
    root: Path,
    trim_mode: str = "center",
    duration_of: Callable[[Path], float] | None = None,
) -> RenderPlan:
    """Normalise edit_plan.json into an ordered, validated clip list. No AI calls."""
    raw_clips = _raw_clips(plan)
    hook_raw, hook_text = _hook_raw(plan, raw_clips)

    clips: list[RenderClip] = []
    dropped: list[str] = []

    hook_clip: RenderClip | None = None
    if hook_raw is not None:
        hook_clip = _build_clip(hook_raw, root, trim_mode, duration_of, True, 0)
        if hook_text:
            hook_clip = RenderClip(**{**hook_clip.__dict__, "caption": hook_text})
        clips.append(hook_clip)

    for index, raw in enumerate(raw_clips):
        clip = _build_clip(raw, root, trim_mode, duration_of, False, index)
        if hook_clip is not None and (
            clip.file == hook_clip.file or clip.clip_id == hook_clip.clip_id
        ):
            dropped.append(clip.clip_id)
            log.info("hook already uses %s, dropping the duplicate body clip", clip.file)
            continue
        clips.append(clip)

    if hook_clip is None and hook_text and clips:
        # Hook text without its own clip: show it over the first clip, hook-styled.
        first = clips[0]
        clips[0] = RenderClip(**{**first.__dict__, "caption": hook_text, "is_hook": True})

    if not clips:
        raise EditPlanError("edit_plan produced no renderable clips")

    plan_date = str(plan.get("date") or date_cls.today().isoformat())
    return RenderPlan(
        date=plan_date,
        story=str(plan.get("story") or ""),
        target_duration=_as_float(plan.get("target_duration")),
        clips=clips,
        dropped_duplicates=dropped,
    )
