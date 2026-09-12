from __future__ import annotations

from pathlib import Path

from dailyreels.cli import format_summary
from dailyreels.manifest import read_manifest, write_manifest
from dailyreels.scanner import scan
from tests.conftest import probe_payload


def _scan(settings, times: dict[str, str]):
    for name in times:
        (settings.root / name).write_bytes(b"x")
    return scan(settings, prober=lambda p: probe_payload(creation_time=times[p.name]))


def test_manifest_json_shape_and_utf8_filenames(make_settings, tmp_path):
    settings = make_settings()
    manifest = _scan(
        settings,
        {
            "출근길.mp4": "2026-09-12T07:03:12.000000Z",
            "IMG_014.mp4": "2026-09-12T18:01:00.000000Z",
        },
    )
    path = write_manifest(manifest, settings.manifest_path)
    payload = read_manifest(path)

    assert path == tmp_path / "data" / "manifest.json"
    assert payload["schema_version"] == 1
    assert payload["root"] == str(settings.root)
    assert [video["file"] for video in payload["videos"]] == ["출근길.mp4", "IMG_014.mp4"]
    assert payload["videos"][0]["captured_at_source"] == "creation_time"
    assert payload["summary"]["video_count"] == 2
    assert payload["summary"]["captured_from"].endswith("07:03:12")
    assert "출근길.mp4" in path.read_text(encoding="utf-8")  # not \u-escaped


def test_write_manifest_is_atomic_and_overwrites(make_settings, tmp_path):
    settings = make_settings()
    manifest = _scan(settings, {"IMG_001.mp4": "2026-09-12T07:03:12.000000Z"})

    write_manifest(manifest, settings.manifest_path)
    write_manifest(manifest, settings.manifest_path)

    data_files = sorted(p.name for p in (tmp_path / "data").iterdir())
    assert data_files == ["manifest.json"]  # no leftover .tmp


def test_format_summary_lists_counts_and_path(make_settings, tmp_path):
    settings = make_settings()
    manifest = _scan(
        settings,
        {
            "IMG_001.mp4": "2026-09-12T07:03:12.000000Z",
            "IMG_002.mp4": "2026-09-12T23:41:00.000000Z",
        },
    )
    text = format_summary(manifest, settings.manifest_path)

    assert "Videos      2" in text
    assert "Captured    07:03 ~ 23:41" in text
    assert "Duration    00:19" in text
    assert "Portrait    2" in text
    assert str(settings.manifest_path) in text
