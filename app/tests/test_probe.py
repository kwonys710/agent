from __future__ import annotations

from datetime import timezone

import pytest

from dailyreels import probe
from tests.conftest import probe_payload


def test_parse_fps_handles_ntsc_fraction():
    assert probe.parse_fps({"avg_frame_rate": "30000/1001"}) == 29.97


def test_parse_fps_falls_back_and_never_divides_by_zero():
    assert probe.parse_fps({"avg_frame_rate": "0/0", "r_frame_rate": "60/1"}) == 60.0
    assert probe.parse_fps({"avg_frame_rate": "0/0", "r_frame_rate": "1/0"}) == 0.0
    assert probe.parse_fps({}) == 0.0


@pytest.mark.parametrize(
    "raw, expected",
    [(90, 90), (-90, 270), (180, 180), ("90", 90), (None, 0), ("weird", 0)],
)
def test_parse_rotation(raw, expected):
    stream = {} if raw is None else {"side_data_list": [{"rotation": raw}]}
    assert probe.parse_rotation(stream) == expected


def test_parse_rotation_reads_legacy_tag():
    assert probe.parse_rotation({"tags": {"rotate": "270"}}) == 270


@pytest.mark.parametrize(
    "width, height, rotation, expected",
    [
        (1080, 1920, 0, "portrait"),
        (1920, 1080, 0, "landscape"),
        (1920, 1080, 90, "portrait"),
        (1080, 1920, 270, "landscape"),
        (1080, 1080, 0, "square"),
        (0, 0, 0, "unknown"),
    ],
)
def test_orientation_accounts_for_rotation(width, height, rotation, expected):
    assert probe.orientation_of(width, height, rotation) == expected


def test_parse_duration_prefers_stream_then_format():
    payload = {"format": {"duration": "12.0"}}
    assert probe.parse_duration(payload, {"duration": "9.5"}) == 9.5
    assert probe.parse_duration(payload, {}) == 12.0
    assert probe.parse_duration({"format": {}}, {"duration": "N/A"}) == 0.0


def test_parse_creation_time_is_utc_aware():
    payload = probe_payload()
    created = probe.parse_creation_time(payload, probe.video_stream(payload))
    assert created is not None
    assert created.tzinfo is not None
    assert created.astimezone(timezone.utc).hour == 7


def test_parse_creation_time_ignores_missing_and_epoch_values():
    assert probe.parse_creation_time(probe_payload(creation_time=None), {}) is None
    epoch = probe_payload(creation_time="1970-01-01T00:00:00.000000Z")
    assert probe.parse_creation_time(epoch, {}) is None


def test_video_stream_skips_audio():
    payload = {"streams": [{"codec_type": "audio"}, {"codec_type": "video", "width": 1}]}
    assert probe.video_stream(payload)["width"] == 1
    assert probe.video_stream({"streams": []}) is None


def test_run_ffprobe_reports_missing_binary():
    with pytest.raises(probe.FFprobeError, match="not found"):
        probe.run_ffprobe(__import__("pathlib").Path("x.mp4"), ffprobe="ffprobe_does_not_exist")
