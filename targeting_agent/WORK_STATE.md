# Targeting Agent — 현재 상태

이 파일은 **현재 상태만** 담는다. 과거 Phase 서술은 쌓지 않는다.
- Phase별 설계/결정 기록: `targeting_agent/docs/phase*.md`
- 재사용할 교훈: `targeting_agent/docs/continuous_improvement.md`
- 개발 운영 규칙: 저장소 루트 `CLAUDE.md`

## Version

v0.3.0 (`project.version` in `targeting_agent/config.yaml`)

## Current Phase

Phase 19 완료 (Multi-Agent Discovery & Engagement Upgrade) + 지속 개선 체계 도입.
운영 관찰 단계이며 새 기능 Phase는 열려 있지 않다.

## Last Completed

- **19N** 작성자 팔로워 규모 수집 (`31b16d9`) — 프로필 `og:description` 문자열 파싱으로
  `creator_fit`이 중립 0.5에 고정되던 문제 해소. 임계값은 바꾸지 않았다.
- **19M** COMMENT Canary (`CommentOnlyExecutor` / `comment_runner`) — LIKE 비활성,
  실행당 1건 상한, `--comment-canary` 직접 실행에서만 동작.
- **Lock 소유자 표기** (`04b40ef`) — 차단 로그에 어느 실행·어느 PID·어느 파일인지와 복구 안내.
- **지속 개선 체계** — `CLAUDE.md`, `.claude/agents/` 7개 역할, ledger 2개 문서.

## Runtime Config (실제 쓰기 관련)

```
autopilot.enabled            = false
scheduler.autopilot          = false
browser_executor.mode        = DRY_RUN
actions.execution.dry_run    = true
browser_discovery.enabled    = false
query_planner.enabled        = true
canary.comment.enabled       = true   # ARMED (운영자 요청)
canary.comment.max_comments_per_run = 1
browser_discovery.profile_meta.enabled = true  (max_profiles_per_run = 10)
```

COMMENT는 ARMED지만 **`--comment-canary`를 직접 실행할 때만** 게시된다.
Scheduler/Autopilot은 이 경로를 호출하지 않는다. 중단은 `enabled=false`.
LIVE 전환은 운영자 판단이며 `browser_executor.mode`와 `actions.execution.dry_run`을
**둘 다** 풀어야 한다.

## Known Operational Issues

1. **Stale lock 수동 정리 필요** — `targeting_agent/data/.browser_discovery.lock`
   (PID 12428, 이미 종료됨). 운영자 PC에서 프로세스 생존 확인 후 파일 삭제.
   로그 표기는 `04b40ef`에서 고쳤지만 남아 있는 파일 자체는 지워야 한다.
2. **19N 팔로워 파싱은 실제 Instagram에서 미검증** — 개발 환경에서 instagram.com이
   차단되어 있어 로컬 DOM fixture로만 확인했다. 실행 로그의
   `프로필 보강: N개 Creator 확인 · 팔로워 확보 M`에서 **M=0이면** 실제 프로필의
   `og:description` 형식이 파서 기대와 다르다는 신호다.
3. **COMMENT Canary는 개발 환경에서 실행 불가** — instagram.com 차단 + 로그인 세션 없음.
   운영자 PC에서만 수행한다 (저장소 루트 `BLOCKED.md` 참조).
4. **사람 Feedback 표본 부족** — 임계값 재검토는 10건 이상 축적 후에만 근거가 생긴다.
   그 전에는 임계값을 건드리지 않는다.

## Last Regression

`python -m pytest targeting_agent/tests -q` → **830 passed** (2026-10-19)

실제 Instagram / Claude CLI 호출 0, Task Scheduler 변경 0.

## Next Recommended Task

1. (운영자) stale lock 삭제 → `.\run_targeting_discovery.bat` 실행 →
   `프로필 보강 … 팔로워 확보 M` 확인 → `--stats`로 82점 이상 후보 확인 →
   `run_targeting_comment_canary.bat --rehearsal`.
2. 위 결과를 가지고 Analytics 읽기 전용 검토 (예시 C) → 개선 후보 Top 3.
3. 관찰 결과가 없으면 신규 Phase를 열지 않는다. 운영 근거 우선.

## Optional / 진행을 막지 않음

- Meta Graph API: 쓰기 미지원 확정. 해시태그 읽기만 가능(권한 심사 필요).
- Gemini 분석/댓글 생성 실사용 검증 (API Key 필요).
- 학습 임계값(`min_keyword_media` 등) 재조정은 운영 데이터 축적 후.
