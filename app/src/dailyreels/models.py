from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

Orientation = Literal["portrait", "landscape", "square", "unknown"]
CapturedAtSource = Literal["creation_time", "file_mtime"]

SCHEMA_VERSION = 1


@dataclass(frozen=True)
class VideoMeta:
    """One source clip, straight from ffprobe. Never modified on disk."""

    file: str
    captured_at: datetime
    captured_at_source: CapturedAtSource
    duration: float
    width: int
    height: int
    fps: float
    codec: str
    orientation: Orientation
    rotation: int
    file_size: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "file": self.file,
            "captured_at": self.captured_at.isoformat(timespec="seconds"),
            "captured_at_source": self.captured_at_source,
            "duration": round(self.duration, 3),
            "width": self.width,
            "height": self.height,
            "fps": self.fps,
            "codec": self.codec,
            "orientation": self.orientation,
            "rotation": self.rotation,
            "file_size": self.file_size,
        }


@dataclass(frozen=True)
class ScanError:
    file: str
    error: str

    def to_dict(self) -> dict[str, Any]:
        return {"file": self.file, "error": self.error}


@dataclass
class Manifest:
    root: str
    videos: list[VideoMeta] = field(default_factory=list)
    errors: list[ScanError] = field(default_factory=list)
    generated_at: datetime | None = None

    @property
    def total_duration(self) -> float:
        return sum(video.duration for video in self.videos)

    def count_orientation(self, orientation: Orientation) -> int:
        return sum(1 for video in self.videos if video.orientation == orientation)

    def summary(self) -> dict[str, Any]:
        captured = [video.captured_at for video in self.videos]
        return {
            "video_count": len(self.videos),
            "captured_from": min(captured).isoformat(timespec="seconds") if captured else None,
            "captured_to": max(captured).isoformat(timespec="seconds") if captured else None,
            "total_duration": round(self.total_duration, 3),
            "portrait": self.count_orientation("portrait"),
            "landscape": self.count_orientation("landscape"),
            "square": self.count_orientation("square"),
            "error_count": len(self.errors),
        }

    def to_dict(self) -> dict[str, Any]:
        generated = self.generated_at or datetime.now().astimezone()
        return {
            "schema_version": SCHEMA_VERSION,
            "generated_at": generated.isoformat(timespec="seconds"),
            "root": self.root,
            "summary": self.summary(),
            "videos": [video.to_dict() for video in self.videos],
            "errors": [error.to_dict() for error in self.errors],
        }
