from __future__ import annotations

import json
import os
from pathlib import Path

from dailyreels.models import Manifest


def write_manifest(manifest: Manifest, path: Path) -> Path:
    """Atomic write: a crash never leaves a half-written manifest behind."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(manifest.to_dict(), ensure_ascii=False, indent=2)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(payload + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return path


def read_manifest(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))
