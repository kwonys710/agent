"""수집 상태 DB 품질검증. 결과는 PASS / FAIL / WARN / BLOCKED 로만 표시하고, 실패를 정상 처리하지 않는다."""
import json
import re

MOJIBAKE = re.compile(r"�|[À-ÿ]{2,}")  # 치환문자 또는 EUC-KR 을 latin-1 로 읽은 흔적


def _meta(conn, key):
    r = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return json.loads(r[0]) if r else None


def validate(conn, start_page=1):
    q = lambda sql, *a: conn.execute(sql, a).fetchone()[0]  # noqa: E731
    res = []
    add = lambda check, status, detail: res.append({"check": check, "status": status, "detail": detail})  # noqa: E731
    mode = _meta(conn, "mode")

    ok_pages = [r[0] for r in conn.execute("SELECT page_number FROM pages WHERE status='ok' ORDER BY 1")]
    expected = list(range(start_page, start_page + len(ok_pages)))
    add("페이지 방문(연속성)", "PASS" if ok_pages and ok_pages == expected else "FAIL",
        f"정상 페이지 {len(ok_pages)}개: {ok_pages[:5]}{'…' if len(ok_pages) > 5 else ''}"
        + ("" if ok_pages == expected else f" / 기대 {expected[:5]} — 건너뛴 페이지 있음"))

    failed = [dict(r) for r in conn.execute("SELECT page_number, error FROM pages WHERE status='failed'")]
    add("실패 페이지", "PASS" if not failed else "FAIL", failed or "없음")

    raw = q("SELECT COALESCE(SUM(row_count),0) FROM pages WHERE status='ok'")
    uniq = q("SELECT COUNT(*) FROM terms")
    dups = q("SELECT COUNT(*) FROM term_dupes")
    add("추출 총건수", "PASS" if raw > 0 else "FAIL", f"추출 {raw}행")
    add("중복 제거 전후", "PASS" if raw == uniq + dups else "FAIL",
        f"제거 전 {raw} / 제거 후 {uniq} / 중복 {dups}")

    for col, name in (("english_term", "영문 용어 누락"), ("korean_term", "한글 대응어 누락")):
        n = q(f"SELECT COUNT(*) FROM terms WHERE {col} IS NULL OR TRIM({col})=''")
        add(name, "PASS" if n == 0 else "FAIL", f"{n}건")

    eds = [r[0] for r in conn.execute("SELECT DISTINCT source_edition FROM terms")]
    if len(eds) > 1:
        add("판본 일관성", "FAIL", f"혼합: {eds}")
    elif eds == ["미확인"] or not eds:
        add("판본 일관성", "WARN", "판본이 화면에서 확인되지 않음(edition_label=미확인)")
    else:
        add("판본 일관성", "PASS", eds[0])

    bad = [dict(r) for r in conn.execute("SELECT term_id, english_term, korean_term FROM terms")
           if MOJIBAKE.search((r["english_term"] or "") + (r["korean_term"] or ""))]
    add("특수문자/인코딩 깨짐", "PASS" if not bad else "FAIL", f"{len(bad)}건" + (f": {bad[:3]}" if bad else ""))

    last = _meta(conn, "last_page_reached")
    if last is not None:
        add("마지막 페이지 도달", "PASS", f"page {last} ({_meta(conn, 'stop_reason')})")
    else:
        add("마지막 페이지 도달", "WARN" if mode == "sample" else "FAIL",
            "샘플 모드 — 전체 페이지 미순회(정상)" if mode == "sample" else "마지막 페이지 미도달")

    total = _meta(conn, "site_total_count")
    if total is None:
        add("사이트 건수 대비 수집 건수", "BLOCKED", "사이트 전체 건수 미확인(total_count_regex 미설정 또는 문구 없음)")
    elif mode == "sample":
        add("사이트 건수 대비 수집 건수", "WARN", f"사이트 {total} / 샘플 추출 {raw}(샘플이므로 비교 대상 아님)")
    else:
        add("사이트 건수 대비 수집 건수", "PASS" if raw == total else "FAIL",
            f"사이트 {total} / 추출 {raw} / 고유 {uniq} — 사이트 건수의 집계 기준(행/표제어) 확인 필요")
    return res
