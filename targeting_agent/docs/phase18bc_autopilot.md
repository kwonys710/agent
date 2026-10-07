# Phase 18B / 18B.1 / 18C / 18C.1 — Action Executor · Autopilot · 품질 모니터링

작성일: 2026-10-07 / 상태: 구현 완료, **실제 공개 쓰기는 비활성(DRY_RUN)**

## 1. 전체 흐름

```
Browser Discovery(18A) → Ingestion(12A) → CandidateProcessor(12B/13)
  → Target Score → 댓글 후보 → Action Policy(18B.1) → Action Queue(v0.1)
  → 승인(Dashboard=REVIEW / 자동=AUTOPILOT) → BrowserExecutor(18B)
  → Interaction · Feedback → Daily/Weekly 리포트 · 품질 모니터링(18C.1)
```

Action Queue / Rate Limiter / Approval Gate / Interaction 기록은 **v0.1 구현을 그대로 쓴다.**
두 번째 Queue도, 두 번째 lock framework도 만들지 않았다.

## 2. Phase 18B — Browser Action Executor

| 항목 | 내용 |
| --- | --- |
| 수행 Action | **LIKE, COMMENT 둘뿐** |
| 미구현(기능 자체 없음) | FOLLOW / DM / SAVE / SHARE |
| Mode | `DISABLED` / `DRY_RUN`(기본) / `LIVE` |
| 강한 가드 | `actions.execution.dry_run=true`면 `LIVE`여도 실제 동작 없음 |
| 중복 방지 | 같은 게시물에 **실제로** 수행한 Action은 재실행하지 않음(Dry Run 기록은 중복 아님) |
| 재시도 | 없음. 실패는 그대로 FAILED로 남긴다 |
| 댓글 | 저장된 댓글 후보만 사용. Executor가 댓글을 만들지 않는다 |
| 중단 | 로그인 필요 / Challenge / 경고 / **작업 차단** 감지 → Action Run 전체 중단 |

selector는 `actions/executors/action_selectors.py` 한 파일에만 둔다. Discovery(읽기)
selector 파일에는 쓰기 selector가 한 줄도 없다(테스트가 검사).

중단 판정은 **명확한 근거**에만 반응한다. 상태 불명(UNKNOWN)으로 승인된 Action 전체가
멈추지 않도록, 로그인 확인은 Run 시작 시 1회(홈 화면)만 엄격하게 한다.

## 3. Phase 18B.1 — Action Policy / 운용 모드

`actions/policy.py`가 "점수 → 추천 Action"을 한 곳에서 정한다. 임계값은 기존 config를
그대로 읽는다(새 임계값을 만들지 않는다).

```
score < minimum_target_score     → 없음
score < require_score_for_like   → 없음
like ≤ score < comment           → LIKE
score ≥ require_score_for_comment→ LIKE + COMMENT (댓글 후보가 있을 때)
```

| 모드 | 승인 | 기본값 |
| --- | --- | --- |
| `REVIEW` | 운영자가 Dashboard에서 승인 | **기본** |
| `AUTOPILOT` | Policy 기준 충족 Action 자동 승인 | `autopilot.enabled=true`일 때만 |

AUTOPILOT에서도 Creator 미확인 / 댓글 텍스트 없음 / 실행당 승인 상한을 지킨다.
Dashboard의 기본 체크 표시도 같은 Policy를 쓴다(화면에는 운영자가 설정한 임계값 기준).

## 4. Phase 18C — Autopilot Orchestration

`python -m targeting_agent.main --autopilot` / `run_targeting_autopilot.bat`

```
lock → Discovery(선택) → inbox → 분석 → Action Queue → 승인 → 실행 → 리포트 → 이력
```

- 단계별 실패는 격리하고, 세션 중단·Action 차단은 전체를 멈춘다.
- 실제 쓰기 건수(`dry_run=0` Interaction)를 따로 집계해 결과에 표시한다.
- `scheduler.autopilot=true`면 Scheduled Run이 Autopilot을 실행한다(기본 false).
- 이력: `autopilot_runs`(schema v10).

## 5. Phase 18C.1 — 운영 품질 모니터링

`learning/operations.py` — Discovery/추출/Executor/Autopilot 지표를 DB에서 계산한다.

- 검색어 실패율, username/caption 추출률, 상세 읽기 실패, 중복률
- Action 실행 성공/실패율, 승인 대기, 실제 쓰기 vs Dry Run, Autopilot 실패·중단
- Score 분포와 Action 추천 분포

`learning/weekly_report.py` — `--weekly-report`로 최근 7일 보고서를 만든다.
**LLM을 호출하지 않는다**(같은 DB면 같은 보고서).
Daily Summary에도 같은 섹션이 들어간다. Dashboard 상단에는 운영 상태 한 줄
(운용 모드 / Executor / Discovery / 사용량 / 최근 실행)을 추가했다.

### Implicit Feedback 분리

| 신호 | 처리 |
| --- | --- |
| 운영자 승인 | 학습 positive 신호(기존 Phase 17 그대로) |
| **Autopilot 자동 승인** | **학습 신호로 쓰지 않는다** — Agent가 자기 결정으로 학습하면 자기 강화가 된다 |
| Skip | 약한 negative(기존) |
| 댓글 수정 | 댓글 품질 지표(comment_stats) |
| Action 실패 | 품질 신호가 아니라 **실행 건강** 지표 |

이를 위해 `action_queue.approved_by`(schema v11)를 추가했다. NULL은 기존 운영자 승인으로 본다.

임계값·Profile은 자동으로 바뀌지 않는다. 보고서는 **추천 문구까지만** 만든다.

## 6. 최종 안전 상태 (개발 완료 시점)

```yaml
autopilot:
  enabled: false          # REVIEW 모드
browser_executor:
  mode: "DRY_RUN"         # 실제 좋아요/댓글 없음
actions:
  execution:
    dry_run: true         # 강한 가드
scheduler:
  autopilot: false
browser_discovery:
  enabled: false          # --discover 로 명시 실행할 때만
```

개발 과정에서 실제 Instagram 게시물에 좋아요·댓글을 **한 건도** 하지 않았다.
LIVE 경로는 FakeActionPage와 **로컬 DOM fixture + 실제 Chromium**으로만 검증했다.

## 7. 실제 Instagram 검증이 남은 부분

개발 환경(Linux 컨테이너)에는 Instagram 로그인 세션이 없고 instagram.com으로의
네트워크 접근도 차단되어 있다. 따라서 아래는 운영자 PC에서만 확인할 수 있다.
`BLOCKED.md` 참고.

1. `run_instagram_session.bat --check` → `LOGGED_IN`
2. `run_targeting_discovery.bat --discover-only --query "직장인"` → 실제 selector 확인
3. `run_targeting_autopilot.bat` → REVIEW + DRY_RUN 전체 흐름
4. (운영자 판단) LIVE 전환
