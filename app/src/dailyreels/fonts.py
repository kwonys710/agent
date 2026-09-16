from __future__ import annotations

import logging
import os
import struct
import sys
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable

log = logging.getLogger(__name__)

ENV_FONT_FILE = "CAPTION_FONT_FILE"
ENV_FONT_NAME = "CAPTION_FONT_NAME"

# Korean-capable families, best first. Paperlogy is the account's brand font.
DEFAULT_PREFERRED = ("Paperlogy", "Malgun Gothic", "Noto Sans KR", "NanumGothic", "AppleSDGothicNeo")
DEFAULT_WEIGHTS = ("SemiBold", "Bold", "Medium", "Regular")
FONT_SUFFIXES = (".ttf", ".otf", ".ttc")


@dataclass(frozen=True)
class FontChoice:
    name: str
    file: Path | None
    source: str  # env_file | env_name | config_file | config_name | preferred | fallback

    @property
    def fonts_dir(self) -> Path | None:
        return self.file.parent if self.file else None


def font_dirs() -> list[Path]:
    dirs: list[Path] = []
    if sys.platform.startswith("win"):
        windir = os.environ.get("WINDIR", r"C:\Windows")
        local = os.environ.get("LOCALAPPDATA", "")
        dirs += [Path(windir) / "Fonts"]
        if local:
            dirs.append(Path(local) / "Microsoft" / "Windows" / "Fonts")
    elif sys.platform == "darwin":
        dirs += [Path("/System/Library/Fonts"), Path("/Library/Fonts"), Path.home() / "Library/Fonts"]
    else:
        dirs += [
            Path("/usr/share/fonts"),
            Path("/usr/local/share/fonts"),
            Path.home() / ".fonts",
            Path.home() / ".local/share/fonts",
        ]
    return [d for d in dirs if d.is_dir()]


def read_family_name(path: Path) -> str | None:
    """Read the family name out of a TrueType/OpenType name table. No dependencies."""
    try:
        with path.open("rb") as handle:
            data = handle.read(4)
            if data == b"ttcf":  # collection: jump to the first font
                handle.seek(12)
                offset = struct.unpack(">I", handle.read(4))[0]
                handle.seek(offset)
            else:
                handle.seek(0)
            header = handle.read(12)
            if len(header) < 12:
                return None
            num_tables = struct.unpack(">H", header[4:6])[0]
            name_offset = None
            for _ in range(num_tables):
                entry = handle.read(16)
                if len(entry) < 16:
                    break
                if entry[:4] == b"name":
                    name_offset = struct.unpack(">I", entry[8:12])[0]
                    break
            if name_offset is None:
                return None

            handle.seek(name_offset)
            count, string_offset = struct.unpack(">HH", handle.read(6)[2:6])
            records = handle.read(12 * count)
            best: tuple[int, str] | None = None
            for index in range(count):
                platform, _enc, _lang, name_id, length, offset = struct.unpack(
                    ">HHHHHH", records[index * 12 : index * 12 + 12]
                )
                if name_id not in (1, 16):
                    continue
                handle.seek(name_offset + string_offset + offset)
                raw = handle.read(length)
                try:
                    text = raw.decode("utf-16-be" if platform in (0, 3) else "latin-1")
                except UnicodeDecodeError:
                    continue
                text = text.strip("\x00").strip()
                if not text:
                    continue
                rank = (2 if name_id == 16 else 1) + (1 if platform == 3 else 0)
                if best is None or rank > best[0]:
                    best = (rank, text)
            return best[1] if best else None
    except (OSError, struct.error, IndexError):
        return None


@lru_cache(maxsize=1)
def _installed_fonts() -> tuple[tuple[Path, str], ...]:
    found: list[tuple[Path, str]] = []
    for directory in font_dirs():
        try:
            for path in sorted(directory.rglob("*")):
                if path.suffix.lower() in FONT_SUFFIXES and path.is_file():
                    found.append((path, path.stem))
        except OSError:
            continue
    return tuple(found)


def find_font_file(family: str, weights: Iterable[str] = DEFAULT_WEIGHTS) -> Path | None:
    """Match a family against installed font files, preferring a readable weight."""
    token = family.replace(" ", "").replace("-", "").lower()
    if not token:
        return None
    weight_order = [w.lower() for w in weights]
    matches: list[tuple[int, Path]] = []
    for path, stem in _installed_fonts():
        if token not in stem.replace(" ", "").replace("-", "").replace("_", "").lower():
            continue
        stem_lower = stem.lower()
        rank = next(
            (index for index, weight in enumerate(weight_order) if weight in stem_lower),
            len(weight_order),
        )
        if "italic" in stem_lower or "oblique" in stem_lower:
            rank += 100
        matches.append((rank, path))
    if not matches:
        return None
    matches.sort(key=lambda item: (item[0], str(item[1])))
    return matches[0][1]


def resolve_font(caption_style: dict[str, Any] | None = None) -> FontChoice:
    """CAPTION_FONT_FILE > CAPTION_FONT_NAME > config > preferred families > fallback."""
    style = caption_style or {}
    preferred = tuple(style.get("preferred_fonts") or DEFAULT_PREFERRED)
    weights = tuple(style.get("font_weights") or DEFAULT_WEIGHTS)

    env_file = os.environ.get(ENV_FONT_FILE) or style.get("font_file")
    if env_file:
        path = Path(str(env_file)).expanduser()
        if path.is_file():
            name = os.environ.get(ENV_FONT_NAME) or style.get("font_name") or read_family_name(path) or path.stem
            return FontChoice(name=str(name), file=path, source="config_file")
        log.warning("caption font file not found: %s", path)

    named = os.environ.get(ENV_FONT_NAME) or style.get("font_name")
    if named:
        path = find_font_file(str(named), weights)
        return FontChoice(name=str(named), file=path, source="config_name")

    for family in preferred:
        path = find_font_file(str(family), weights)
        if path is not None:
            return FontChoice(name=read_family_name(path) or str(family), file=path, source="preferred")

    fallback = str(preferred[-1] if preferred else "Malgun Gothic")
    log.warning("no preferred caption font found, falling back to %s by name", fallback)
    return FontChoice(name=fallback, file=None, source="fallback")
