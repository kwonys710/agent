# Phase 19 Task Board (v0.3.0)

| Phase | 내용 | 상태 |
| --- | --- | --- |
| 19.0 | Multi-Agent Foundation / Architecture 문서 | DONE |
| 19A | Query Bank (seed 70 / 카테고리 10) | DONE |
| 19B | Query Performance / Utility / Rotation / Cooldown | DONE |
| 19C | Dynamic Query Expansion (batch, 결정론적) | DONE |
| 19D | Novelty Engine (교차 검색어 attribution 포함) | DONE |
| 19E | Freshness Discovery (posted_at 우선순위) | DONE |
| 19F | Comment Engagement (기존 파이프라인 연결) | DONE |
| 19G | Comment Verification (own comment + refresh) | DONE |
| 19H | Activity Controller (감소 전용) | DONE |
| 19I | Action Mix (LIKE / LIKE+COMMENT) | DONE |
| 19J | Scheduler → Query Planner 연결 | DONE |
| 19K | Dashboard Discovery Health / Query / Engagement | DONE |
| 19L | E2E + Release Readiness Report + v0.3.0 | DONE |

병렬: 19G(Comment browser path) ∥ 19A·19B·19D(Query/Novelty) — 파일 교집합 없음.
순차: schema / database / config / policy / executor 를 건드리는 작업.

## 실기 확인이 남은 것 (운영자 PC)

| 항목 | 이유 |
| --- | --- |
| 실제 Instagram read-only smoke | 개발 환경은 instagram.com 차단 + 로그인 세션 없음(BLOCKED.md) |
| 실제 COMMENT Canary | §33/§68 — 코드는 LIVE_READY, 실제 공개 댓글은 Operator Gate |
