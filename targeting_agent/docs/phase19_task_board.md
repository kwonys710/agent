# Phase 19 Task Board (v0.3.0)

| Phase | 내용 | 상태 |
| --- | --- | --- |
| 19.0 | Multi-Agent Foundation / Architecture 문서 | DONE |
| 19A | Query Bank (seed 50+, 상태·성과 컬럼) | READY |
| 19B | Query Performance / Utility / Rotation / Cooldown | BLOCKED_BY_19A |
| 19C | Dynamic Query Expansion (batch) | BLOCKED_BY_19B |
| 19D | Novelty Engine (first/last_seen, seen_count, queries_found_by) | BLOCKED_BY_19A |
| 19E | Freshness Discovery (posted_at 우선순위) | BLOCKED_BY_19D |
| 19F | Comment Engagement (기존 comment 파이프라인 연결) | READY |
| 19G | Comment Verification (own comment + refresh, UNKNOWN 무재시도) | READY |
| 19H | Activity Controller (NORMAL/CAUTION/STOPPED, 감소 전용) | BLOCKED_BY_19F |
| 19I | Action Mix (LIKE / LIKE+COMMENT) | BLOCKED_BY_19F |
| 19J | Scheduler → Query Planner 연결 | BLOCKED_BY_19B |
| 19K | Dashboard Discovery Health / Query / Engagement | BLOCKED_BY_19B,19D |
| 19L | E2E + Release Readiness Report + v0.3.0 | BLOCKED_BY_ALL |

병렬: 19G(Comment browser path) ∥ 19A·19B·19D(Query/Novelty) — 파일 교집합 없음.
순차: schema / database / config / policy / executor 를 건드리는 작업.
