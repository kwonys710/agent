"""로깅 설정.

- 콘솔 + 일자별 파일 로그(UTF-8)
- logging.keep_days 를 넘긴 오래된 로그 파일은 실행 시 정리
- print 대신 logging을 사용한다(실행 Summary 출력만 예외).
"""
from __future__ import annotations

import logging
import sys
import time
from pathlib import Path
from typing import Optional

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
_CONFIGURED = False


def setup_logging(
    level: str = "INFO",
    log_dir: Optional[Path] = None,
    keep_days: int = 30,
    *,
    force: bool = False,
) -> logging.Logger:
    """루트 로거를 구성하고 targeting_agent 로거를 반환한다."""
    global _CONFIGURED
    root = logging.getLogger()
    if _CONFIGURED and not force:
        return logging.getLogger("targeting_agent")

    for handler in list(root.handlers):
        root.removeHandler(handler)

    root.setLevel(getattr(logging, str(level).upper(), logging.INFO))
    formatter = logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT)

    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(formatter)
    root.addHandler(stream)

    if log_dir is not None:
        log_dir = Path(log_dir)
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / f"targeting_{time.strftime('%Y%m%d')}.log"
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)
        _cleanup_old_logs(log_dir, keep_days)

    _CONFIGURED = True
    return logging.getLogger("targeting_agent")


def _cleanup_old_logs(log_dir: Path, keep_days: int) -> None:
    if keep_days <= 0:
        return
    cutoff = time.time() - keep_days * 86400
    for path in log_dir.glob("targeting_*.log"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
        except OSError:  # pragma: no cover - 파일 잠김 등
            continue


def get_logger(name: str) -> logging.Logger:
    """모듈용 로거(`targeting_agent.<name>`)를 반환한다."""
    return logging.getLogger(f"targeting_agent.{name}")
