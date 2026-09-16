from __future__ import annotations

from pathlib import Path

import pytest

from dailyreels.config import Settings


def probe_payload(
    *,
    width: int = 1080,
    height: int = 1920,
    rotation: int | None = None,
    creation_time: str | None = "2026-09-12T07:03:12.000000Z",
    duration: str | None = "9.512",
    codec: str = "h264",
    fps: str = "30000/1001",
    codec_type: str = "video",
) -> dict:
    stream: dict = {
        "codec_type": codec_type,
        "codec_name": codec,
        "width": width,
        "height": height,
        "avg_frame_rate": fps,
        "r_frame_rate": fps,
    }
    if duration is not None:
        stream["duration"] = duration
    if rotation is not None:
        stream["side_data_list"] = [{"side_data_type": "Display Matrix", "rotation": rotation}]

    payload: dict = {"streams": [stream], "format": {"duration": duration, "tags": {}}}
    if creation_time is not None:
        payload["format"]["tags"]["creation_time"] = creation_time
    return payload


@pytest.fixture
def make_settings(tmp_path: Path):
    def _make(**overrides) -> Settings:
        root = overrides.pop("root", tmp_path)
        defaults = dict(
            root=root,
            data_dir=root / "data",
            output_dir=root / "output",
            logs_dir=root / "logs",
            timezone="UTC",
        )
        defaults.update(overrides)
        return Settings(**defaults)

    return _make
