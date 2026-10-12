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

## 원격 분기 통합 (2026-10-12)

Phase 19는 `002ccb8`(18A.5) 기준으로 작업했고, 그 사이 원격에 18A.6 / 18D.x / 18E /
18E.1이 올라와 있었다. `cfe4c18`에서 merge로 합쳤다(force push·history 변경 없음).

| 충돌 | 해결 |
| --- | --- |
| `discovery/browser_instagram.py` | 18A.6의 2026 DOM 대응 우선(shortcode 중복 판정, 상세 확인 후 media_type) + 그 위에 19D 교차 attribution 재적용 |
| `WORK_STATE.md` | 양쪽 Phase 기록 모두 보존 |

원격에 있던 테스트 실패 4건(`test_canary_resume`)도 함께 고쳤다 — merge로 생긴 것이
아니라 원래 빨간 상태였고(깨끗한 worktree에서 동일 실패 확인), `canary/resume.py`의
`_ai_usage`가 호출자가 넘긴 시각을 무시하고 실제 날짜로 사용량을 조회한 것이 원인이다.
