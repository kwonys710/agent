"""Claude Code CLI Runner (Phase 13B).

로컬에 설치·로그인된 Claude Code CLI를 subprocess로 호출한다.
LLM API SDK(OpenAI/Gemini/Anthropic)를 사용하지 않으며,
Credential이나 session token을 직접 읽거나 저장하지 않는다 —
Claude Code가 자체 관리하는 인증을 그대로 쓴다.

Runtime 호출 원칙:
- 후보 1개당 최대 1회 호출. 같은 요청을 재시도하지 않는다.
- 프로젝트 Repository를 Context로 넘기지 않는다
  (작업 디렉터리를 빈 임시 폴더로 두어 CLAUDE.md/파일 탐색을 차단).
- 개발용 세션을 이어받지 않는다(`--continue`/`--resume`를 쓰지 않는다).
- 파일을 고치거나 명령을 실행할 도구가 필요 없으므로 최소 권한으로 실행한다
  (`--restricted`, `--disallowed-tools`). `--dangerously-skip-permissions`는 쓰지 않는다.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Sequence

from ..core.logger import get_logger
from .schema import FailureReason, TargetingAnalysis, parse_analysis

logger = get_logger("ai.claude_runner")

CLI_NAME = "claude"
# Runtime 분석에는 필요 없는 도구들(프로젝트 변경/외부 접근 계열)
DEFAULT_DISALLOWED_TOOLS = (
    "Bash",
    "Edit",
    "Write",
    "NotebookEdit",
    "WebFetch",
    "WebSearch",
    "Task",
)
USAGE_LIMIT_MARKERS = ("usage limit", "rate limit", "quota", "too many requests")


@dataclass
class RunnerResult:
    """Claude 호출 1회의 결과."""

    ok: bool
    analysis: Optional[TargetingAnalysis] = None
    failure: Optional[FailureReason] = None
    detail: str = ""
    duration_ms: int = 0
    cost_usd: float = 0.0
    num_turns: int = 0
    raw_result: str = ""


@dataclass
class ClaudeRunner:
    """`claude -p`를 호출해 JSON 결과를 돌려준다."""

    model: str = "sonnet"
    max_turns: int = 1
    timeout_seconds: int = 120
    executable: Optional[str] = None
    disallowed_tools: Sequence[str] = field(default=DEFAULT_DISALLOWED_TOOLS)
    restricted: bool = True

    def resolve_executable(self) -> Optional[str]:
        return self.executable or shutil.which(CLI_NAME)

    def available(self) -> bool:
        """CLI가 설치되어 있는지만 확인한다(호출하지 않는다)."""
        return self.resolve_executable() is not None

    def build_command(self, executable: str) -> list[str]:
        command = [
            executable,
            "-p",
            "--output-format",
            "json",
            "--max-turns",
            str(self.max_turns),
            "--model",
            self.model,
            "--strict-mcp-config",
        ]
        if self.restricted:
            # 명령 실행 계열 내장 도구를 제거한다.
            command.append("--restricted")
        if self.disallowed_tools:
            command += ["--disallowed-tools", " ".join(self.disallowed_tools)]
        return command

    def run(self, prompt: str) -> RunnerResult:
        """프롬프트 1건을 실행한다. 실패해도 재시도하지 않는다."""
        executable = self.resolve_executable()
        if executable is None:
            return RunnerResult(
                ok=False,
                failure=FailureReason.CLAUDE_UNAVAILABLE,
                detail=f"'{CLI_NAME}' 실행 파일을 찾을 수 없습니다.",
            )

        # 프롬프트는 stdin으로 넘긴다.
        # `--disallowed-tools`가 가변 인자라서 뒤에 붙인 프롬프트를 인자로 삼켜버린다
        # ("Input must be provided either through stdin or as a prompt argument").
        command = self.build_command(executable)
        # Repository를 Context로 넘기지 않기 위해 빈 임시 디렉터리에서 실행한다.
        with tempfile.TemporaryDirectory(prefix="targeting_claude_") as workdir:
            try:
                completed = subprocess.run(  # noqa: S603 - 고정된 CLI만 실행
                    command,
                    cwd=workdir,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=self.timeout_seconds,
                    input=prompt,
                    env=self._child_env(),
                    check=False,
                )
            except subprocess.TimeoutExpired:
                logger.warning("Claude 호출 timeout(%ds) — heuristic으로 대체합니다.", self.timeout_seconds)
                return RunnerResult(
                    ok=False,
                    failure=FailureReason.TIMEOUT,
                    detail=f"{self.timeout_seconds}s 초과",
                )
            except OSError as exc:  # pragma: no cover - 실행 환경 문제
                return RunnerResult(
                    ok=False, failure=FailureReason.CLAUDE_UNAVAILABLE, detail=str(exc)[:200]
                )

        return self._interpret(completed.returncode, completed.stdout, completed.stderr)

    @staticmethod
    def _child_env() -> dict[str, str]:
        """자식 프로세스 환경. Credential을 새로 주입하지 않는다."""
        env = dict(os.environ)
        env.setdefault("CLAUDE_CODE_NONINTERACTIVE", "1")
        return env

    def _interpret(self, returncode: int, stdout: str, stderr: str) -> RunnerResult:
        combined = f"{stderr}\n{stdout}".lower()
        if any(marker in combined for marker in USAGE_LIMIT_MARKERS):
            return RunnerResult(
                ok=False, failure=FailureReason.USAGE_LIMIT, detail=(stderr or stdout)[:200]
            )
        if returncode != 0:
            return RunnerResult(
                ok=False,
                failure=FailureReason.CLI_ERROR,
                detail=f"exit={returncode} {(stderr or stdout)[:200]}",
            )

        wrapper = self._parse_wrapper(stdout)
        if wrapper is None:
            return RunnerResult(
                ok=False, failure=FailureReason.INVALID_JSON, detail=(stdout or "")[:200]
            )
        if wrapper.get("is_error"):
            return RunnerResult(
                ok=False,
                failure=FailureReason.CLI_ERROR,
                detail=str(wrapper.get("result") or wrapper.get("subtype"))[:200],
            )

        # wrapper 성공과 payload 유효성은 별개다. payload를 반드시 검증한다.
        raw_result = str(wrapper.get("result") or "")
        analysis, failure, detail = parse_analysis(raw_result)
        meta = {
            "duration_ms": int(wrapper.get("duration_ms") or 0),
            "cost_usd": float(wrapper.get("total_cost_usd") or 0.0),
            "num_turns": int(wrapper.get("num_turns") or 0),
        }
        if analysis is None:
            logger.warning("Claude 응답 검증 실패(%s) — heuristic으로 대체합니다.", failure)
            return RunnerResult(
                ok=False, failure=failure, detail=detail, raw_result=raw_result[:500], **meta
            )
        return RunnerResult(ok=True, analysis=analysis, raw_result=raw_result[:500], **meta)

    @staticmethod
    def _parse_wrapper(stdout: str) -> Optional[dict[str, Any]]:
        """CLI wrapper JSON을 읽는다(앞쪽 경고 문구가 섞여도 허용)."""
        if not stdout:
            return None
        start = stdout.find("{")
        if start == -1:
            return None
        try:
            payload = json.loads(stdout[start:])
        except json.JSONDecodeError:
            return None
        return payload if isinstance(payload, dict) else None


def build_prompt(template_path: Path | str, profile_text: str, candidate_json: str) -> str:
    """프롬프트 파일에 Profile과 후보 정보만 채운다(다른 파일은 넘기지 않는다)."""
    template = Path(template_path).read_text(encoding="utf-8")
    return template.replace("{profile}", profile_text).replace("{candidate}", candidate_json)
