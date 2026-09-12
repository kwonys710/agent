from __future__ import annotations

import logging
import sys
from datetime import date
from pathlib import Path

_CONFIGURED = False


def setup_logging(logs_dir: Path | None = None, verbose: bool = False) -> None:
    """Console + optional daily file logging. Safe to call more than once."""
    global _CONFIGURED
    if _CONFIGURED:
        return

    root = logging.getLogger("dailyreels")
    root.setLevel(logging.DEBUG if verbose else logging.INFO)

    console = logging.StreamHandler(stream=sys.stderr)
    console.setLevel(logging.DEBUG if verbose else logging.WARNING)
    console.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    root.addHandler(console)

    if logs_dir is not None:
        try:
            logs_dir.mkdir(parents=True, exist_ok=True)
            handler = logging.FileHandler(
                logs_dir / f"dailyreels_{date.today().isoformat()}.log",
                encoding="utf-8",
            )
            handler.setLevel(logging.DEBUG)
            handler.setFormatter(
                logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
            )
            root.addHandler(handler)
        except OSError:  # read-only or unavailable drive: console logging still works
            root.warning("could not open log file in %s", logs_dir)

    _CONFIGURED = True
