"""Candidate Ingestion (Phase 12A).

입력 → 검증 → 정규화 → 중복 확인 → DB 저장 → 결과 기록.

두 가지 입력 경로를 담당한다.
1. CLI 단건/다건:  `--add <Instagram URL>`
2. Inbox CSV:      `data/inbox/*.csv`

원칙:
- 한 건의 실패가 전체를 중단시키지 않는다(행/URL 단위 격리).
- 원본 파일을 삭제하지 않는다. 처리 후 `processed/`(또는 `failed/`)로 옮긴다.
- 행 일부만 실패하면 파일은 processed로 보내고 실패 내역을 Report로 남긴다.
- 페이지 접근/스크래핑/로그인은 하지 않는다. URL에 없는 값은 추측하지 않는다.
"""
from __future__ import annotations

import csv
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence

from ..core.database import insert_candidate, record_import_event, transaction
from ..core.exceptions import DiscoveryError
from ..core.logger import get_logger
from ..core.models import MediaStatus, RawCandidate
from .import_source import row_to_candidate
from .url_input import SOURCE_CSV_INBOX, SOURCE_MANUAL_URL, candidate_from_url

logger = get_logger("discovery.ingest")

ADDED, DUPLICATE, INVALID, ERROR = "ADDED", "DUPLICATE", "INVALID", "ERROR"

URL_COLUMNS = ("url", "permalink", "link")
CSV_ENCODINGS = ("utf-8-sig", "utf-8", "cp949")  # Windows에서 저장한 CSV 대응


@dataclass
class ImportResult:
    """입력 1건의 처리 결과."""

    result: str
    original_url: str = ""
    canonical_url: Optional[str] = None
    media_pk: Optional[int] = None
    error_message: Optional[str] = None
    source_file: Optional[str] = None
    source_row: Optional[int] = None
    note: str = ""


@dataclass
class ImportSummary:
    """여러 건의 처리 결과 집계."""

    input_count: int = 0
    added: int = 0
    duplicate: int = 0
    invalid: int = 0
    error: int = 0
    needs_enrichment: int = 0
    files_processed: list[str] = field(default_factory=list)
    files_failed: list[str] = field(default_factory=list)
    details: list[ImportResult] = field(default_factory=list)

    def record(self, item: ImportResult) -> None:
        self.input_count += 1
        self.details.append(item)
        if item.result == ADDED:
            self.added += 1
        elif item.result == DUPLICATE:
            self.duplicate += 1
        elif item.result == INVALID:
            self.invalid += 1
        else:
            self.error += 1

    def merge(self, other: "ImportSummary") -> None:
        self.input_count += other.input_count
        self.added += other.added
        self.duplicate += other.duplicate
        self.invalid += other.invalid
        self.error += other.error
        self.needs_enrichment += other.needs_enrichment
        self.files_processed += other.files_processed
        self.files_failed += other.files_failed
        self.details += other.details

    @property
    def has_input(self) -> bool:
        return self.input_count > 0


def initial_status(candidate: RawCandidate) -> MediaStatus:
    """분석에 쓸 내용이 없으면 NEEDS_ENRICHMENT로 둔다(파이프라인을 막지 않기 위함)."""
    if candidate.caption.strip() or candidate.hashtags:
        return MediaStatus.NEW
    return MediaStatus.NEEDS_ENRICHMENT


class CandidateIngestor:
    """URL/CSV 입력을 후보로 저장한다."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    # --- 단건 ----------------------------------------------------------
    def add_url(
        self,
        raw_url: str,
        *,
        username: Optional[str] = None,
        caption: str = "",
        note: str = "",
        source: str = SOURCE_MANUAL_URL,
        source_file: Optional[str] = None,
        source_row: Optional[int] = None,
        summary: Optional[ImportSummary] = None,
    ) -> ImportResult:
        """URL 한 건을 후보로 저장하고 결과를 기록한다."""
        try:
            candidate = candidate_from_url(
                raw_url, username=username, caption=caption, note=note, source=source
            )
        except DiscoveryError as exc:
            item = ImportResult(
                result=INVALID,
                original_url=raw_url,
                error_message=str(exc),
                source_file=source_file,
                source_row=source_row,
                note=note,
            )
            self._persist_event(item, source)
            logger.warning("입력 거부(%s): %s", raw_url, exc)
            if summary is not None:
                summary.record(item)
            return item

        return self.add_candidate(
            candidate,
            original_url=raw_url,
            note=note,
            source=source,
            source_file=source_file,
            source_row=source_row,
            summary=summary,
        )

    def add_candidate(
        self,
        candidate: RawCandidate,
        *,
        original_url: str = "",
        note: str = "",
        source: Optional[str] = None,
        source_file: Optional[str] = None,
        source_row: Optional[int] = None,
        summary: Optional[ImportSummary] = None,
    ) -> ImportResult:
        """이미 만들어진 후보를 저장한다(CSV의 상세 컬럼 경로에서도 사용)."""
        source_name = source or candidate.source
        status = initial_status(candidate)
        try:
            with transaction(self.conn):
                media_pk = insert_candidate(self.conn, candidate, status=status)
            if media_pk is None:
                item = ImportResult(
                    result=DUPLICATE,
                    original_url=original_url or candidate.permalink,
                    canonical_url=candidate.canonical_url,
                    source_file=source_file,
                    source_row=source_row,
                    note=note or candidate.note,
                )
            else:
                item = ImportResult(
                    result=ADDED,
                    original_url=original_url or candidate.permalink,
                    canonical_url=candidate.canonical_url,
                    media_pk=media_pk,
                    source_file=source_file,
                    source_row=source_row,
                    note=note or candidate.note,
                )
                if summary is not None and status is MediaStatus.NEEDS_ENRICHMENT:
                    summary.needs_enrichment += 1
        except Exception as exc:  # noqa: BLE001 - 입력 1건의 실패를 격리한다
            logger.exception("후보 저장 실패: %s", original_url or candidate.permalink)
            item = ImportResult(
                result=ERROR,
                original_url=original_url or candidate.permalink,
                canonical_url=candidate.canonical_url,
                error_message=str(exc)[:200],
                source_file=source_file,
                source_row=source_row,
            )

        self._persist_event(item, source_name)
        if summary is not None:
            summary.record(item)
        return item

    def add_urls(
        self, urls: Sequence[str], *, username: Optional[str] = None, note: str = ""
    ) -> ImportSummary:
        summary = ImportSummary()
        for url in urls:
            self.add_url(url, username=username, note=note, summary=summary)
        return summary

    def _persist_event(self, item: ImportResult, source: str) -> None:
        try:
            with transaction(self.conn):
                record_import_event(
                    self.conn,
                    source=source,
                    result=item.result,
                    original_url=item.original_url,
                    canonical_url=item.canonical_url,
                    media_pk=item.media_pk,
                    source_file=item.source_file,
                    source_row=item.source_row,
                    error_message=item.error_message,
                    note=item.note,
                )
        except sqlite3.Error:  # pragma: no cover - 기록 실패가 입력을 막지 않게 한다
            logger.warning("import_events 기록 실패(입력 처리는 계속): %s", item.original_url)

    # --- Inbox CSV ------------------------------------------------------
    def process_inbox(
        self,
        inbox_dir: Path | str,
        *,
        processed_dir: Optional[Path | str] = None,
        failed_dir: Optional[Path | str] = None,
        write_report: bool = True,
    ) -> ImportSummary:
        """`inbox/*.csv`를 처리하고 파일을 processed/ 또는 failed/로 옮긴다."""
        inbox = Path(inbox_dir)
        summary = ImportSummary()
        if not inbox.exists():
            return summary

        processed = Path(processed_dir) if processed_dir else inbox / "processed"
        failed = Path(failed_dir) if failed_dir else inbox / "failed"

        for path in sorted(inbox.glob("*.csv")):
            if not path.is_file():
                continue
            file_summary = ImportSummary()
            try:
                rows = self._read_csv(path)
            except DiscoveryError as exc:
                # 파일 자체를 읽을 수 없거나 구조가 잘못된 경우에만 failed로 보낸다.
                logger.error("CSV를 읽을 수 없습니다(%s): %s", path.name, exc)
                file_summary.record(
                    ImportResult(result=ERROR, error_message=str(exc), source_file=path.name)
                )
                self._persist_event(file_summary.details[-1], SOURCE_CSV_INBOX)
                summary.merge(file_summary)
                summary.files_failed.append(self._move(path, failed).name)
                continue

            for index, row in enumerate(rows, start=2):  # 헤더가 1행
                self._process_row(row, path, index, file_summary)

            if write_report and (file_summary.invalid or file_summary.error):
                self._write_report(path, file_summary)

            summary.merge(file_summary)
            summary.files_processed.append(self._move(path, processed).name)
            logger.info(
                "Inbox 처리 %s: 입력 %d / 추가 %d / 중복 %d / 무효 %d / 오류 %d",
                path.name,
                file_summary.input_count,
                file_summary.added,
                file_summary.duplicate,
                file_summary.invalid,
                file_summary.error,
            )
        return summary

    def _process_row(
        self, row: Mapping[str, Any], path: Path, index: int, summary: ImportSummary
    ) -> None:
        data = {str(k).strip().lower(): v for k, v in row.items() if k}
        url = next((str(data[c]).strip() for c in URL_COLUMNS if data.get(c)), "")
        username = str(data.get("username") or "").strip()
        note = str(data.get("note") or "").strip()
        source = str(data.get("source") or "").strip() or SOURCE_CSV_INBOX

        # username + media_id/permalink 가 모두 있으면 v0.1 Import 경로를 재사용한다.
        if username and (data.get("media_id") or url):
            try:
                candidate = row_to_candidate({**data, "permalink": url}, source=source)
                candidate.canonical_url = candidate_from_url(
                    url or candidate.permalink, username=username, source=source
                ).canonical_url if url else None
                self.add_candidate(
                    candidate,
                    original_url=url,
                    source=source,
                    source_file=path.name,
                    source_row=index,
                    summary=summary,
                )
                return
            except DiscoveryError as exc:
                logger.warning("%s %d행 상세 파싱 실패, URL 경로로 재시도: %s", path.name, index, exc)

        if not url:
            item = ImportResult(
                result=INVALID,
                error_message="url 컬럼이 비어 있습니다.",
                source_file=path.name,
                source_row=index,
            )
            self._persist_event(item, source)
            summary.record(item)
            return

        self.add_url(
            url,
            username=username or None,
            caption=str(data.get("caption") or ""),
            note=note,
            source=source,
            source_file=path.name,
            source_row=index,
            summary=summary,
        )

    def _read_csv(self, path: Path) -> list[dict[str, Any]]:
        last_error: Optional[Exception] = None
        for encoding in CSV_ENCODINGS:
            try:
                with path.open("r", encoding=encoding, newline="") as handle:
                    reader = csv.DictReader(handle)
                    if not reader.fieldnames:
                        raise DiscoveryError("헤더가 없습니다.")
                    headers = {str(h).strip().lower() for h in reader.fieldnames if h}
                    if not (headers & set(URL_COLUMNS)) and "media_id" not in headers:
                        raise DiscoveryError(
                            f"필수 컬럼이 없습니다. 필요한 컬럼: {', '.join(URL_COLUMNS)}"
                        )
                    return [dict(row) for row in reader]
            except UnicodeDecodeError as exc:
                last_error = exc
                continue
        raise DiscoveryError(f"인코딩을 해석할 수 없습니다: {last_error}")

    @staticmethod
    def _move(path: Path, target_dir: Path) -> Path:
        """원본을 삭제하지 않고 옮긴다. 같은 이름이 있으면 접미사를 붙인다."""
        target_dir.mkdir(parents=True, exist_ok=True)
        destination = target_dir / path.name
        if destination.exists():
            stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
            destination = target_dir / f"{path.stem}_{stamp}{path.suffix}"
        path.replace(destination)
        return destination

    @staticmethod
    def _write_report(path: Path, summary: ImportSummary) -> Path:
        """행 일부가 실패했을 때 파일 옆에 결과 리포트를 남긴다."""
        report_path = path.with_name(f"{path.stem}_report.md")
        lines = [
            f"# Import Report — {path.name}",
            "",
            f"- 입력 {summary.input_count} / 추가 {summary.added} / 중복 {summary.duplicate} "
            f"/ 무효 {summary.invalid} / 오류 {summary.error}",
            "",
            "| 행 | 결과 | URL | 사유 |",
            "| --- | --- | --- | --- |",
        ]
        for item in summary.details:
            if item.result in (ADDED, DUPLICATE):
                continue
            lines.append(
                f"| {item.source_row or '-'} | {item.result} | {item.original_url} "
                f"| {item.error_message or ''} |"
            )
        report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return report_path


def format_summary(summary: ImportSummary) -> str:
    """CLI 출력용 ADD SUMMARY."""
    lines = [
        "",
        "ADD SUMMARY",
        "",
        f"  Input       {summary.input_count}",
        f"  Added       {summary.added}",
        f"  Duplicate   {summary.duplicate}",
        f"  Invalid     {summary.invalid}",
        f"  Error       {summary.error}",
    ]
    if summary.needs_enrichment:
        lines.append(f"  (보강 필요   {summary.needs_enrichment} — caption/hashtag 없음)")
    if summary.files_processed:
        lines.append(f"  처리한 파일  {', '.join(summary.files_processed)}")
    if summary.files_failed:
        lines.append(f"  실패한 파일  {', '.join(summary.files_failed)}")
    for item in summary.details:
        if item.result in (INVALID, ERROR):
            location = f"{item.source_file}:{item.source_row}" if item.source_file else "CLI"
            lines.append(f"    - [{item.result}] {location} {item.original_url} — {item.error_message}")
    return "\n".join(lines)
