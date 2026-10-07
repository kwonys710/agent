"""Phase 13A — Claude Code Runtime Probe.

로컬 Claude Code CLI로 비대화형 실행이 되는지만 확인한다.
Instagram 접근 없음, DB 수정 없음, 파일 수정 없음, 외부 Action 없음.

실행:
    python -m targeting_agent.scripts.probe_claude_runtime
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Optional

if __package__ in (None, ""):  # pragma: no cover
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    __package__ = "targeting_agent.scripts"

from ..ai.claude_runner import ClaudeRunner
from ..ai.schema import parse_analysis

MARKER = "CLAUDE_RUNTIME_OK"


def check_cli(runner: ClaudeRunner) -> tuple[bool, str]:
    executable = runner.resolve_executable()
    if executable is None:
        return False, "claude 실행 파일을 찾을 수 없습니다(PATH 확인)."
    try:
        version = subprocess.run(  # noqa: S603
            [executable, "--version"],
            capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:  # pragma: no cover
        return False, f"버전 확인 실패: {exc}"
    return version.returncode == 0, (version.stdout or version.stderr).strip()[:80]


def check_text_mode(runner: ClaudeRunner) -> tuple[bool, str]:
    """`claude -p ... --max-turns 1` 최소 실행."""
    executable = runner.resolve_executable()
    if executable is None:
        return False, "CLI 없음"
    try:
        completed = subprocess.run(  # noqa: S603
            [executable, "-p", f"Return exactly: {MARKER}", "--max-turns", "1",
             "--model", runner.model, "--strict-mcp-config"],
            capture_output=True, text=True, timeout=runner.timeout_seconds,
            stdin=subprocess.DEVNULL, check=False,
        )
    except subprocess.TimeoutExpired:
        return False, f"timeout({runner.timeout_seconds}s)"
    output = (completed.stdout or "").strip()
    return MARKER in output, output[:120] or (completed.stderr or "")[:120]


def check_json_mode(runner: ClaudeRunner, prompt_path: Optional[Path]) -> tuple[bool, str]:
    """JSON wrapper + payload 검증까지 확인한다."""
    if prompt_path is None or not prompt_path.exists():
        return False, f"프롬프트 파일 없음: {prompt_path}"
    from ..ai.claude_runner import build_prompt

    profile = "- 평일: 직장인, 출근, 퇴근, 점심\n- 주말: 카페, 맛집, 산책\n- 언어: ko"
    candidate = json.dumps(
        {
            "username": "commute_story",
            "caption": "출근길 버스에서 보는 아침 하늘 #출근 #직장인일상",
            "hashtags": ["출근", "직장인일상"],
            "media_type": "REEL",
            "followers": 4400,
        },
        ensure_ascii=False,
    )
    result = runner.run(build_prompt(prompt_path, profile, candidate))
    if not result.ok or result.analysis is None:
        return False, f"{result.failure} {result.detail[:120]}"
    analysis = result.analysis
    detail = (
        f"relevance={analysis.relevance_score}, topic={analysis.primary_topic}, "
        f"댓글 {len(analysis.comment_candidates)}개, {result.duration_ms}ms, "
        f"${result.cost_usd:.4f}"
    )
    return True, detail


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Phase 13A Claude Runtime Probe")
    parser.add_argument("--model", default="sonnet")
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--skip-json", action="store_true", help="JSON 모드 검증 생략(호출 1회 절약)")
    args = parser.parse_args(argv)

    runner = ClaudeRunner(model=args.model, timeout_seconds=args.timeout)
    prompt_path = Path(__file__).resolve().parents[1] / "prompts" / "targeting_analysis_v1.md"

    rows: list[tuple[str, bool, str]] = []
    ok, detail = check_cli(runner)
    rows.append(("CLI 설치/버전", ok, detail))

    if ok:
        ok_text, detail_text = check_text_mode(runner)
        rows.append(("claude -p 최소 실행", ok_text, detail_text))
        if ok_text and not args.skip_json:
            ok_json, detail_json = check_json_mode(runner, prompt_path)
            rows.append(("JSON 모드 + 스키마 검증", ok_json, detail_json))

    print("\n| 검증항목 | 결과 | 상세 |")
    print("| --- | --- | --- |")
    for name, passed, detail in rows:
        print(f"| {name} | {'PASS' if passed else 'FAIL'} | {detail} |")

    all_passed = all(passed for _, passed, _ in rows)
    print(f"\n판정: {'GO' if all_passed else 'NO-GO'}")
    if not all_passed:
        print("실패 시 API 방식으로 전환하지 않는다. Claude Code 로그인/설치 상태를 확인하라.")
    return 0 if all_passed else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
