"""Browser Discovery Run 실행기 (Phase 18A).

한 번 실행되면 아래를 하고 종료한다.

  lock → 세션 확인 → 검색 Discovery → CandidateIngestor 저장 → CandidateProcessor 분석 → 이력 기록

지키는 것:
- **Instagram 쓰기 동작을 하지 않는다.** 브라우저는 열기/검색/스크롤/읽기만 한다.
- Discovery는 Claude / Scorer / Comment Generator를 **직접 호출하지 않는다.**
  분석은 기존 CandidateProcessor(Phase 12B)에만 맡긴다.
- 새로 저장된 후보(ADDED)만 분석한다 — Duplicate는 다시 분석하지 않는다(Token Guard).
- 로그인 필요 / Challenge / 경고가 감지되면 즉시 중단하고, 자동으로 해결하려 하지 않는다.
- Scheduler와 자동 연결하지 않는다(운영자가 명시적으로 실행할 때만 동작).
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Optional, Sequence

from ..core.config import Config
from ..core.database import bump_stat, get_connection, init_db, today_str, utc_now
from ..core.exceptions import BrowserSessionError, TargetingError
from ..core.logger import get_logger
from ..scheduler.lock import SchedulerLock
from .browser_models import SOURCE_BROWSER_SEARCH, SessionState
from .ingest import ADDED, CandidateIngestor, ImportSummary

logger = get_logger("discovery.browser_runner")

# Run 상태(18A.1). 검색어가 전부 실패했는데 OK로 남던 문제를 고친다.
STATUS_SUCCESS = "SUCCESS"          # 실행한 검색어가 모두 성공
STATUS_PARTIAL = "PARTIAL"          # 성공/실패 검색어가 섞임
STATUS_FAILED = "FAILED"            # 실행한 검색어가 전부 실패(또는 실행 자체 실패)
STATUS_SESSION_STOPPED = "SESSION_STOPPED"
STATUS_LOCKED = "LOCKED"
STATUS_DISABLED = "DISABLED"
# 이전 이름(OK)은 SUCCESS와 같은 의미로 남겨 둔다.
STATUS_OK = STATUS_SUCCESS


@dataclass
class BrowserDiscoveryResult:
    """Browser Discovery Run 1회 결과."""

    status: str = STATUS_SUCCESS
    started_at: str = ""
    finished_at: str = ""
    session_state: str = SessionState.UNKNOWN.value
    queries: int = 0
    found: int = 0
    collected: int = 0
    added: int = 0
    duplicate: int = 0
    invalid: int = 0
    analyzed: int = 0
    needs_enrichment: int = 0
    claude_calls: int = 0
    cache_hits: int = 0
    other_errors: int = 0      # 저장/분석 단계 오류
    selector_errors: int = 0   # 화면 구조(selector) 오류
    ok_queries: int = 0
    failed_queries: int = 0
    stop_reason: Optional[str] = None
    message: str = ""
    added_media_pks: list[int] = field(default_factory=list)
    details: list[str] = field(default_factory=list)

    @property
    def errors(self) -> int:
        """화면 오류 + 처리 오류의 총합(요약의 '오류' 숫자와 세부 항목을 일치시킨다)."""
        return self.other_errors + self.selector_errors

    @property
    def exit_code(self) -> int:
        """후보 1건의 실패는 실패로 보지 않는다. 실행이 목적을 이루지 못했을 때만 non-zero."""
        if self.status in (STATUS_SUCCESS, STATUS_PARTIAL, STATUS_DISABLED, STATUS_LOCKED):
            return 0
        return 1


BrowserFactory = Callable[[Config], Any]
ProcessorFactory = Callable[[Config, sqlite3.Connection], Any]


def _default_browser(config: Config) -> Any:
    """설정대로 Playwright 브라우저를 만든다(운영자가 로그인한 프로필 재사용)."""
    from .browser_instagram import PlaywrightBrowser

    browser = PlaywrightBrowser(
        profile_dir=config._resolve_path(
            config.get("browser_discovery.profile_dir", "data/browser_profile")
        ),
        headless=bool(config.get("browser_discovery.headless", False)),
        timeout_ms=int(config.get("browser_discovery.timeout_ms", 20000)),
        selector_timeout_ms=int(config.get("browser_discovery.selector_timeout_ms", 4000)),
        debug_dir=(
            config._resolve_path(config.get("browser_discovery.debug.dir", "data/browser_debug"))
            if bool(config.get("browser_discovery.debug.enabled", True))
            else None
        ),
        max_screenshots=int(config.get("browser_discovery.debug.max_screenshots", 10)),
        scroll_rounds=int(config.get("browser_discovery.scroll_rounds", 3)),
    )
    browser.start()
    return browser


def _default_processor(config: Config, conn: sqlite3.Connection) -> Any:
    """Phase 12B의 CandidateProcessor를 그대로 쓴다(한도 가드를 실행 내내 공유)."""
    from ..ai.intelligence import ClaudeIntelligence
    from ..analysis.profile_analyzer import load_profile
    from ..pipeline import CandidateProcessor

    profile = load_profile(config.profile_path, conn)
    return CandidateProcessor(
        config, conn, profile=profile, intelligence=ClaudeIntelligence(config, profile)
    )


def queries_from_config(config: Config) -> list[str]:
    """검색어 목록. 명시된 queries가 없으면 기존 해시태그 설정을 재사용한다."""
    raw = config.get("browser_discovery.queries", None)
    if not raw:
        raw = config.get("discovery.hashtags", [])
    return [str(q).strip() for q in (raw or []) if str(q).strip()]


def run_browser_discovery(
    config: Config,
    conn: Optional[sqlite3.Connection] = None,
    *,
    browser: Optional[Any] = None,
    browser_factory: BrowserFactory = _default_browser,
    processor_factory: ProcessorFactory = _default_processor,
    queries: Optional[Sequence[str]] = None,
    process: bool = True,
    now: Optional[datetime] = None,
) -> BrowserDiscoveryResult:
    """Browser Discovery 1회. 예외를 밖으로 내보내지 않고 결과로 돌려준다."""
    now = now or datetime.now(timezone.utc)
    result = BrowserDiscoveryResult(started_at=now.isoformat(timespec="seconds"))

    if not bool(config.get("browser_discovery.enabled", False)):
        result.status = STATUS_DISABLED
        result.message = (
            "browser_discovery.enabled=false — 아무 것도 하지 않았습니다. "
            "config.yaml에서 명시적으로 켜야 동작합니다."
        )
        result.finished_at = utc_now()
        return result

    lock = SchedulerLock(
        path=config._resolve_path(
            config.get("browser_discovery.lock.path", "data/.browser_discovery.lock")
        ),
        stale_minutes=int(config.get("browser_discovery.lock.stale_minutes", 30)),
        enabled=bool(config.get("browser_discovery.lock.enabled", True)),
    )
    if not lock.acquire(now):
        result.status = STATUS_LOCKED
        result.message = "다른 Browser Discovery가 진행 중이어서 종료합니다."
        result.finished_at = utc_now()
        logger.info(result.message)
        return result

    owns_connection = conn is None
    owns_browser = browser is None
    try:
        if conn is None:
            conn = get_connection(config.db_path)
            init_db(conn)

        query_list = list(queries) if queries else queries_from_config(config)
        if not query_list:
            result.status = STATUS_FAILED
            result.message = "검색어가 없습니다(browser_discovery.queries / discovery.hashtags)."
            logger.warning(result.message)
            return result

        if browser is None:
            browser = browser_factory(config)

        candidates = _discover(config, browser, query_list, result)
        _ingest(conn, candidates, result)
        if process and result.added_media_pks:
            _process(config, conn, result, processor_factory)

        _record_daily_stats(conn, config, result)
    except BrowserSessionError as exc:
        # 로그인/Challenge/경고 — 자동으로 풀지 않고 여기서 멈춘다.
        result.status = STATUS_SESSION_STOPPED
        result.session_state = exc.state
        result.stop_reason = exc.state
        result.message = str(exc)
        logger.warning("Browser Discovery 중단(%s): %s", exc.state, exc)
    except TargetingError as exc:
        result.status = STATUS_FAILED
        result.message = str(exc)[:300]
        logger.error("Browser Discovery 실패: %s", exc)
    except Exception as exc:  # noqa: BLE001 - 실행 자체의 실패만 non-zero로 만든다
        logger.exception("Browser Discovery 실패")
        result.status = STATUS_FAILED
        result.message = str(exc)[:300]
    finally:
        result.finished_at = utc_now()
        if owns_browser and browser is not None:
            try:
                browser.close()
            except Exception:  # noqa: BLE001 - 브라우저 종료 실패는 결과를 바꾸지 않는다
                logger.warning("브라우저 종료 실패(무시)")
        if conn is not None:
            try:
                _record_run(conn, result)
            except sqlite3.Error:  # pragma: no cover - 이력 기록 실패가 Run을 실패로 만들지 않는다
                logger.warning("Discovery 이력 기록 실패(무시)")
        lock.release()
        if owns_connection and conn is not None:
            conn.close()

    logger.info(
        "Browser Discovery 종료: status=%s 발견 %d / 수집 %d / 신규 %d / 중복 %d, 분석 %d, Claude %d회",
        result.status,
        result.found,
        result.collected,
        result.added,
        result.duplicate,
        result.analyzed,
        result.claude_calls,
    )
    return result


# --- 단계 ----------------------------------------------------------------
def _discover(
    config: Config, browser: Any, query_list: Sequence[str], result: BrowserDiscoveryResult
) -> list[Any]:
    from .browser_instagram import InstagramBrowserDiscovery

    discovery = InstagramBrowserDiscovery(
        browser,
        query_list,
        max_queries_per_run=int(config.get("browser_discovery.limits.max_queries_per_run", 5)),
        max_candidates_per_query=int(
            config.get("browser_discovery.limits.max_candidates_per_query", 10)
        ),
        max_candidates_per_run=int(
            config.get("browser_discovery.limits.max_candidates_per_run", 40)
        ),
        media_types=config.get("browser_discovery.media_types", ["REEL"]) or ["REEL"],
        stop_on_login_required=bool(
            config.get("browser_discovery.stop_on.login_required", True)
        ),
        stop_on_challenge=bool(config.get("browser_discovery.stop_on.challenge", True)),
        stop_on_warning=bool(config.get("browser_discovery.stop_on.platform_warning", True)),
    )
    candidates = discovery.discover()
    stats = discovery.stats
    result.session_state = stats.session_state.value
    result.queries = len(stats.queries)
    result.found = stats.found
    result.collected = stats.collected
    result.selector_errors = stats.selector_errors
    result.ok_queries = stats.ok_queries
    result.failed_queries = stats.failed_queries
    result.status = _status_of(stats.ok_queries, stats.failed_queries)
    for query in stats.queries:
        for message in query.errors:
            result.details.append(f"query={query.query} {message}")
    return candidates


def _status_of(ok_queries: int, failed_queries: int) -> str:
    """검색어 성공/실패 비율로 Run 상태를 정한다(전부 실패인데 OK로 남지 않게 한다)."""
    if failed_queries and ok_queries:
        return STATUS_PARTIAL
    if failed_queries:
        return STATUS_FAILED
    return STATUS_SUCCESS


def _ingest(
    conn: sqlite3.Connection, candidates: Sequence[Any], result: BrowserDiscoveryResult
) -> ImportSummary:
    """Phase 12A의 CandidateIngestor를 그대로 쓴다(중복 판정·import_events 기록 동일)."""
    ingestor = CandidateIngestor(conn)
    summary = ImportSummary()
    for candidate in candidates:
        item = ingestor.add_candidate(
            candidate,
            original_url=candidate.permalink,
            note=candidate.note,
            source=SOURCE_BROWSER_SEARCH,
            summary=summary,
        )
        if item.result == ADDED and item.media_pk is not None:
            result.added_media_pks.append(int(item.media_pk))
    result.added = summary.added
    result.duplicate = summary.duplicate
    result.invalid = summary.invalid
    result.other_errors += summary.error
    return summary


def _process(
    config: Config,
    conn: sqlite3.Connection,
    result: BrowserDiscoveryResult,
    processor_factory: ProcessorFactory,
) -> None:
    """신규 후보만 CandidateProcessor로 넘긴다. Claude 한도는 Processor가 관리한다."""
    limit = int(config.get("browser_discovery.limits.max_analyze_per_run", 20))
    targets = result.added_media_pks[:limit] if limit else list(result.added_media_pks)
    if not targets:
        return

    processor = processor_factory(config, conn)
    for media_pk in targets:
        try:
            outcome = processor.process(media_pk)
        except Exception as exc:  # noqa: BLE001 - 후보 1건 실패가 Run을 멈추지 않게 한다
            logger.exception("후보 처리 실패: media_pk=%s", media_pk)
            conn.rollback()
            result.other_errors += 1
            result.details.append(f"media_pk={media_pk} ERROR {str(exc)[:80]}")
            continue
        if outcome.status == "ERROR":
            result.other_errors += 1
            result.details.append(f"media_pk={media_pk} ERROR {outcome.detail[:80]}")
        elif outcome.status == "NEEDS_ENRICHMENT":
            result.needs_enrichment += 1
        else:
            result.analyzed += 1

    usage = getattr(processor.intelligence, "usage", None)
    if usage is not None:
        result.claude_calls = usage.claude_calls
        result.cache_hits = usage.cache_hits


def _record_daily_stats(
    conn: sqlite3.Connection, config: Config, result: BrowserDiscoveryResult
) -> None:
    date = today_str(int(config.get("actions.daily_limits.timezone_offset_hours", 9)))
    if result.added:
        bump_stat(conn, date, "discovered", result.added)
    if result.analyzed:
        bump_stat(conn, date, "analyzed", result.analyzed)
    if result.cache_hits:
        bump_stat(conn, date, "claude_cache_hits", result.cache_hits)
    conn.commit()


def _record_run(conn: sqlite3.Connection, result: BrowserDiscoveryResult) -> None:
    conn.execute(
        "INSERT INTO browser_discovery_runs (started_at, finished_at, status, session_state, "
        "queries, found, collected, added, duplicate, invalid, analyzed, needs_enrichment, "
        "claude_calls, cache_hits, errors, stop_reason) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            result.started_at,
            result.finished_at,
            result.status,
            result.session_state,
            result.queries,
            result.found,
            result.collected,
            result.added,
            result.duplicate,
            result.invalid,
            result.analyzed,
            result.needs_enrichment,
            result.claude_calls,
            result.cache_hits,
            result.errors,
            result.stop_reason,
        ),
    )
    conn.commit()


def format_result(result: BrowserDiscoveryResult) -> str:
    """CLI 출력용 요약(한 화면)."""
    lines = [
        "",
        "=" * 52,
        " Instagram Browser Discovery",
        "=" * 52,
        f" 상태        : {result.status}",
        f" 세션        : {result.session_state}",
        f" 검색어      : {result.queries}개 (성공 {result.ok_queries} / 실패 {result.failed_queries})",
        f" 발견 / 수집 : {result.found} / {result.collected}",
        f" 신규 / 중복 : {result.added} / {result.duplicate}",
        f" 분석 완료   : {result.analyzed} (정보 부족 {result.needs_enrichment})",
        f" Claude 호출 : {result.claude_calls} (Cache Hit {result.cache_hits})",
        f" 오류        : {result.errors}",
        f"   - Selector : {result.selector_errors}",
        f"   - 기타     : {result.other_errors}",
    ]
    if result.stop_reason:
        lines.append(f" 중단 사유   : {result.stop_reason}")
    if result.message:
        lines.append(f" 메시지      : {result.message}")
    if result.details:
        lines.append(" 상세        :")
        lines.extend(f"   - {item}" for item in result.details[:10])
    lines.append("=" * 52)
    return "\n".join(lines)
