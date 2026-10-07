"""CSV / JSON / URL 목록 기반 후보 Import.

v0.1의 기본 Discovery. 수집 자체를 자동화하지 않고,
운영자가 직접 모은 Reel 목록을 파이프라인에 넣는 경로다.

지원 포맷:
- .csv : 헤더 필요. media_id 또는 permalink 중 하나는 반드시 있어야 한다.
- .json: 위 CSV와 동일한 key를 가진 객체 배열
- .txt : Instagram permalink 한 줄에 하나
"""
from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any, Mapping, Optional

from ..core.exceptions import DiscoveryError
from ..core.logger import get_logger
from ..core.models import RawCandidate

from .base import DiscoverySource, extract_hashtags, shortcode_from_permalink

logger = get_logger("discovery.import")

TRUE_VALUES = {"1", "true", "yes", "y", "t", "참"}


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in TRUE_VALUES


def _as_int(value: Any) -> int:
    try:
        return int(float(str(value).replace(",", "").strip()))
    except (TypeError, ValueError):
        return 0


def _split_hashtags(value: Any, caption: str) -> list[str]:
    if value:
        raw = str(value).replace("#", " ").replace(",", " ")
        tags = [t.strip().lower() for t in raw.split() if t.strip()]
        if tags:
            return tags
    return extract_hashtags(caption)


def row_to_candidate(row: Mapping[str, Any], source: str = "import") -> RawCandidate:
    """CSV/JSON 한 행을 RawCandidate로 정규화한다."""
    data = {str(k).strip().lower(): v for k, v in row.items() if k}
    permalink = str(data.get("permalink") or data.get("url") or "").strip()
    media_id = str(data.get("media_id") or "").strip()
    if not media_id:
        media_id = shortcode_from_permalink(permalink) or ""
    if not media_id:
        raise DiscoveryError(f"media_id/permalink를 확인할 수 없는 행: {dict(row)!r}")

    username = str(data.get("username") or data.get("creator") or "").strip().lstrip("@")
    if not username:
        raise DiscoveryError(f"username이 비어 있는 행: media_id={media_id}")

    caption = str(data.get("caption") or "")
    return RawCandidate(
        media_id=media_id,
        permalink=permalink or f"https://www.instagram.com/reel/{media_id}/",
        username=username,
        caption=caption,
        hashtags=_split_hashtags(data.get("hashtags"), caption),
        media_type=str(data.get("media_type") or "REEL").upper(),
        like_count=_as_int(data.get("like_count")),
        comment_count=_as_int(data.get("comment_count")),
        posted_at=str(data.get("posted_at") or "").strip() or None,
        language=(str(data.get("language") or "").strip() or None),
        followers=_as_int(data.get("followers")),
        is_private=_as_bool(data.get("is_private")),
        is_ad=_as_bool(data.get("is_ad")),
        source=source,
    )


class ImportDiscovery(DiscoverySource):
    """파일에서 후보를 읽어오는 Discovery 소스."""

    name = "import"

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    def available(self) -> bool:
        return self.path.exists()

    def discover(self, limit: Optional[int] = None) -> list[RawCandidate]:
        if not self.path.exists():
            raise DiscoveryError(f"Import 파일을 찾을 수 없습니다: {self.path}")

        suffix = self.path.suffix.lower()
        if suffix == ".csv":
            rows = self._read_csv()
        elif suffix == ".json":
            rows = self._read_json()
        elif suffix == ".txt":
            rows = self._read_urls()
        else:
            raise DiscoveryError(f"지원하지 않는 Import 포맷: {suffix} ({self.path})")

        candidates: list[RawCandidate] = []
        for index, row in enumerate(rows, start=1):
            try:
                candidates.append(row_to_candidate(row, source=f"import:{self.path.name}"))
            except DiscoveryError as exc:
                logger.warning("Import %s행 건너뜀: %s", index, exc)
            if limit and len(candidates) >= limit:
                break

        logger.info("Import Discovery: %s에서 후보 %d건 로드", self.path.name, len(candidates))
        return candidates

    def _read_csv(self) -> list[Mapping[str, Any]]:
        # utf-8-sig: Excel에서 저장한 CSV의 BOM 대응(Windows 환경 고려)
        with self.path.open("r", encoding="utf-8-sig", newline="") as handle:
            return [dict(row) for row in csv.DictReader(handle)]

    def _read_json(self) -> list[Mapping[str, Any]]:
        data = json.loads(self.path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data = data.get("items", [])
        if not isinstance(data, list):
            raise DiscoveryError("JSON Import는 객체 배열이어야 합니다.")
        return data

    def _read_urls(self) -> list[Mapping[str, Any]]:
        rows: list[Mapping[str, Any]] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            url = line.strip()
            if not url or url.startswith("#"):
                continue
            shortcode = shortcode_from_permalink(url)
            if not shortcode:
                logger.warning("permalink 형식이 아님: %s", url)
                continue
            rows.append({"permalink": url, "media_id": shortcode, "username": f"unknown_{shortcode}"})
        return rows
