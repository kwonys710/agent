from __future__ import annotations

import argparse
import sys

from dailyreels import __version__
from dailyreels.config import ConfigError, load_settings
from dailyreels.logging_setup import setup_logging
from dailyreels.manifest import write_manifest
from dailyreels.models import Manifest
from dailyreels.scanner import scan


def _hms(seconds: float) -> str:
    total = int(round(seconds))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def format_summary(manifest: Manifest, manifest_path) -> str:
    summary = manifest.summary()
    lines = ["", "DailyReels", ""]
    lines.append(f"Videos      {summary['video_count']}")

    if manifest.videos:
        first = min(video.captured_at for video in manifest.videos)
        last = max(video.captured_at for video in manifest.videos)
        lines.append(f"Captured    {first:%H:%M} ~ {last:%H:%M}")
        lines.append(f"Duration    {_hms(summary['total_duration'])}")
        lines.append(f"Portrait    {summary['portrait']}")
        if summary["landscape"]:
            lines.append(f"Landscape   {summary['landscape']}")
        if summary["square"]:
            lines.append(f"Square      {summary['square']}")

    if manifest.errors:
        lines.append("")
        lines.append(f"Skipped     {len(manifest.errors)}")
        for error in manifest.errors:
            lines.append(f"  {error.file}: {error.error}")

    lines.append("")
    lines.append("Manifest created:")
    lines.append(str(manifest_path))
    lines.append("")
    return "\n".join(lines)


def cmd_scan(args: argparse.Namespace) -> int:
    settings = load_settings(root=args.root)
    setup_logging(settings.logs_dir, verbose=args.verbose)

    manifest = scan(settings)
    manifest_path = write_manifest(manifest, settings.manifest_path)

    if not manifest.videos:
        print(f"\nNo videos found in {settings.root}")
        print(f"Looked for: {', '.join(settings.video_extensions)} (root folder only)\n")
        return 1

    print(format_summary(manifest, manifest_path))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dailyreels",
        description="Turn a day of phone clips into one Instagram Reel.",
    )
    parser.add_argument("--version", action="version", version=f"dailyreels {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    scan_parser = subparsers.add_parser(
        "scan", help="Probe root-level videos and write data/manifest.json"
    )
    scan_parser.add_argument("--root", default=None, help="DailyReels root folder")
    scan_parser.add_argument("-v", "--verbose", action="store_true")
    scan_parser.set_defaults(func=cmd_scan)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except ConfigError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
