"""Runtime Claude 결과 캐시 (Phase 13).

동일 Candidate + 동일 Prompt Version + 동일 Model이면 Claude를 다시 호출하지 않는다.
Cache Key = SHA256(model + prompt_version + normalized_candidate_input)
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any, Mapping, Optional

from ..core.database import utc_now
from .schema import TargetingAnalysis

# 캐시 키에 쓰는 후보 입력 필드(여기에 없는 값이 바뀌어도 재호출하지 않는다)
CACHE_INPUT_FIELDS = ("username", "caption", "hashtags", "media_type", "followers")


def normalized_candidate_input(media: Mapping[str, Any]) -> str:
    """후보 입력을 캐시 키용으로 정규화한다(키 순서 고정, 공백 정리)."""
    payload: dict[str, Any] = {}
    for field in CACHE_INPUT_FIELDS:
        value = media.get(field)
        if isinstance(value, str):
            value = " ".join(value.split())
        elif isinstance(value, (list, tuple)):
            value = [str(v).strip().lower() for v in value]
        payload[field] = value
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def cache_key(model: str, prompt_version: str, media: Mapping[str, Any]) -> str:
    raw = f"{model}|{prompt_version}|{normalized_candidate_input(media)}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def get_cached(conn: sqlite3.Connection, key: str) -> Optional[TargetingAnalysis]:
    row = conn.execute(
        "SELECT payload FROM ai_analysis_cache WHERE cache_key = ?", (key,)
    ).fetchone()
    if row is None:
        return None
    try:
        return TargetingAnalysis.model_validate(json.loads(row["payload"]))
    except Exception:  # noqa: BLE001 - 캐시가 깨졌으면 없는 것으로 취급
        return None


def save_cached(
    conn: sqlite3.Connection,
    key: str,
    *,
    model: str,
    prompt_version: str,
    media_pk: Optional[int],
    analysis: TargetingAnalysis,
) -> bool:
    """캐시에 저장한다. 캐시는 최적화이므로 실패해도 파이프라인을 막지 않는다."""
    try:
        return _write(conn, key, model, prompt_version, media_pk, analysis)
    except sqlite3.IntegrityError:
        # media_pk가 아직 없거나 삭제된 경우 — 참조 없이 저장한다.
        conn.rollback()
        try:
            return _write(conn, key, model, prompt_version, None, analysis)
        except sqlite3.Error:
            conn.rollback()
            return False
    except sqlite3.Error:
        conn.rollback()
        return False


def _write(
    conn: sqlite3.Connection,
    key: str,
    model: str,
    prompt_version: str,
    media_pk: Optional[int],
    analysis: TargetingAnalysis,
) -> bool:
    conn.execute(
        "INSERT INTO ai_analysis_cache (cache_key, model, prompt_version, media_pk, payload, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(cache_key) DO UPDATE SET payload = excluded.payload, created_at = excluded.created_at",
        (
            key,
            model,
            prompt_version,
            media_pk,
            analysis.model_dump_json(),
            utc_now(),
        ),
    )
    conn.commit()
    return True
