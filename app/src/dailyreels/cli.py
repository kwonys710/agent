from __future__ import annotations

import argparse
import sys
from pathlib import Path

from dailyreels import __version__
from dailyreels.config import ConfigError, load_settings
from dailyreels.logging_setup import setup_logging
from dailyreels.manifest import write_manifest
from dailyreels.edit_plan import EditPlanError
from dailyreels.models import Manifest
from dailyreels.probe import FFprobeError
from dailyreels.renderer import RenderError, RenderResult, render
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


def format_render_report(result: RenderResult) -> str:
    plan = result.plan
    hook = plan.hook
    lines = ["", "DailyReels — FINAL REEL CREATED", ""]
    lines.append(f"Output      {result.output}")
    lines.append(f"Resolution  {result.width}x{result.height}")
    lines.append(f"Duration    {result.duration:.1f} sec")
    lines.append(f"FPS         {result.fps:g}")
    lines.append(f"Codec       {result.codec}")
    lines.append(f"Size        {result.file_size / (1024 * 1024):.1f} MB")
    lines.append(f"Font        {result.font.name}")
    lines.append("")
    if plan.story:
        lines.append(f"Story       {plan.story}")
    if hook is not None:
        lines.append(f"Hook        {hook.file}  {hook.caption}")
    lines.append(f"Body clips  {len(plan.body)}")
    for clip in plan.body:
        label = f"{clip.time_label} " if clip.time_label else ""
        lines.append(
            f"  {label}{clip.file}  {clip.start:.1f}s +{clip.use_duration:.1f}s  {clip.caption}"
        )
    if plan.dropped_duplicates:
        lines.append("")
        lines.append(f"Skipped (hook duplicate)  {', '.join(plan.dropped_duplicates)}")
    if result.previews:
        lines.append("")
        lines.append("Preview frames:")
        for path in result.previews:
            lines.append(f"  {path}")
    lines.append("")
    return "\n".join(lines)


def cmd_render(args: argparse.Namespace) -> int:
    settings = load_settings(root=args.root)
    setup_logging(settings.logs_dir, verbose=args.verbose)

    plan_path = Path(args.plan) if args.plan else None
    try:
        result = render(
            settings,
            plan_path=plan_path,
            keep_temp=args.keep_temp,
            previews=not args.no_preview,
        )
    except (EditPlanError, RenderError, FFprobeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print(format_render_report(result))
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

    render_parser = subparsers.add_parser(
        "render", help="Render data/edit_plan.json into output/DailyReel_<date>.mp4"
    )
    render_parser.add_argument("--root", default=None, help="DailyReels root folder")
    render_parser.add_argument("--plan", default=None, help="edit_plan.json path")
    render_parser.add_argument("--keep-temp", action="store_true", help="keep render_tmp segments")
    render_parser.add_argument("--no-preview", action="store_true", help="skip preview frames")
    render_parser.add_argument("-v", "--verbose", action="store_true")
    render_parser.set_defaults(func=cmd_render)

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
