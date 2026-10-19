# Phase 19 — Multi-Agent Discovery & Engagement Architecture (v0.3.0)

작성일: 2026-10-12 / 기준 커밋: 002ccb8 (Phase 18A.5)

## 0. 시작 시점의 실제 상태 (중요)

이번 작업 지시는 "Phase 12A~18E.1 완료"를 전제했지만, 이 저장소의 실제 HEAD는
**Phase 18A.5**다. `18D` / `18E` 문서도, `canary` 심볼도 없다. 따라서 Phase 19는
아래 실제 구현 위에 올린다.

| 전제했던 것 | 실제 | Phase 19의 처리 |
| --- | --- | --- |
| Phase 18D posted_at | `candidate_media.posted_at`은 Phase 2부터 존재하고 `activity_score`가 사용 중 | 그 컬럼을 그대로 Freshness에 사용(19E) |
| LIKE LIVE Canary | 없음. LIKE는 `browser_executor.mode=DRY_RUN` + `actions.execution.dry_run=true` | **그 상태를 바꾸지 않는다**(활동량 확대 없음) |
| Phase 18E.1 | 없음 | 해당 없음 |

## 1. Runtime 파이프라인 (Phase 19 완료 시)

```
Target Profile
  → Query Planner(19B)  ← Query Bank(19A) ← Query Expansion(19C)
  → Query Batch(회전·쿨다운)
  → Browser Discovery(18A)
  → Novelty Gate(19D) ─ 반복 발견은 metadata만 갱신(Claude 0, Action 0)
  → Freshness 우선순위(19E)
  → Candidate Ingestion(12A)
  → Claude Intelligence(13) → Target Score(3)
  → Engagement Policy(19I) ─ Comment Quality Gate(19F)
  → Action Queue(5) → Activity Controller(19H) → Safety Gate(RateLimiter)
  → Browser Lock → BrowserExecutor(18B) ─ Comment Verification(19G)
  → Interaction → Metrics(19K) → Query Learning(19B) / Target Learning(17)
```

## 2. Runtime Writer는 항상 하나

개발은 Multi-Agent지만, 실제 Instagram write 경로는 **하나뿐**이다.

```
Autopilot/Scheduler → Safety Gate(RateLimiter + ActivityController)
  → SchedulerLock(browser lock) → BrowserExecutor → PlaywrightActionPage
```

Discovery / Comment / Engagement 계층은 각자 브라우저를 띄우지 않는다.
Discovery는 읽기 전용 브라우저, Executor는 쓰기 경로이며 둘 다 lock 아래에서만 돈다.

## 3. 모듈 소유권 (개발 Agent → 파일)

| Agent | 파일 |
| --- | --- |
| Discovery | `discovery/query_bank.py`, `query_planner.py`, `query_expansion.py`, `browser_runner.py` |
| Novelty | `discovery/novelty.py`, `core/database.py`(additive) |
| Intelligence | 기존 `ai/`, `analysis/` 재사용(변경 최소) |
| Comment | `comments/*`(재사용), `actions/executors/browser_page.py`, `action_selectors.py` |
| Engagement | `actions/policy.py`, `actions/queue.py` |
| Safety | `actions/activity.py`, `actions/rate_limiter.py`(재사용) |
| Analytics | `dashboard/*`, `learning/operations.py`, `learning/release_report.py` |
| QA | `tests/*` |

`core/schema.py`, `core/database.py`, `config.yaml`, `actions/policy.py`,
`actions/executors/*`는 충돌 위험이 높아 **순차 수정**한다(§3 정책).

## 4. 학습 분리

| 학습 | 자동 적용 | 근거 |
| --- | --- | --- |
| Query Learning(19B) | 가능 — utility 갱신 / cooldown / EXPERIMENTAL↔ACTIVE | Query 성과는 되돌리기 쉽고 영구 삭제하지 않는다 |
| Target Learning(17) | **금지** — preview 후 명시적 apply | Profile은 사람의 판단 영역 |

Query 성과가 좋다고 topic weight를 같은 값만큼 올리지 않는다(두 학습은 입력도 출력도 분리).
Agent의 LIKE/COMMENT와 Autopilot 자동 승인은 Human Ground Truth가 아니다(18C.1 유지).

## 5. 안전 불변식

- 탐지/CAPTCHA/Challenge/Action Block/Rate Limit 우회 없음. stealth·fingerprint·proxy·
  계정/UA rotation 없음. 비공식 API·GraphQL·응답 가로채기·쿠키/토큰 추출 없음.
- 경고·Challenge·Action Block 감지 시 활동량은 **감소 방향으로만** 변한다(19H).
- 자동 증가 알고리즘 없음. 운영자가 정한 상한이 천장이다.
- Query 회전은 **Discovery 다양성** 목적이며 탐지 회피 수단이 아니다.
- Phase 19 종료 상태에서 COMMENT는 `LIVE_READY`까지. 실제 공개 댓글은 Operator Gate.
