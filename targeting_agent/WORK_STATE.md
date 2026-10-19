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

**Last Improvement Cycle: CI-001** (OMC Verification + Operational Reliability)
완료: P1-1 (lock 생존 판정) · P1-2 (enrichment 관측성) · P2-3 (score 입력 결측 관측성)

## Last Completed

- **19N** 작성자 팔로워 규모 수집 (`31b16d9`) — 프로필 `og:description` 문자열 파싱으로
  `creator_fit`이 중립 0.5에 고정되던 문제 해소. 임계값은 바꾸지 않았다.
- **19M** COMMENT Canary (`CommentOnlyExecutor` / `comment_runner`) — LIKE 비활성,
  실행당 1건 상한, `--comment-canary` 직접 실행에서만 동작.
- **지속 개선 체계** — `CLAUDE.md`, `.claude/agents/` 7개 역할, ledger 2개 문서.
- **CI-001 / P1-1** (`12cde06`, `b5e9397`) — lock 판정을 커널 파일락
  (`fcntl.flock` / `msvcrt`)으로 옮겼다. 죽은 소유자의 lock 이 TTL(30/60분)까지
  실행을 막던 문제가 사라졌다. PID 조회 설계는 Safety Reviewer 가 기각했다
  (2중 실행 + 측정 불가 전제).
- **CI-001 / P1-2** (`f9e7b70`, `fb032e5`) — 프로필 보강 결과를 사유별 8개 버킷으로
  쪼개고 전멸 시 경고한다. 비공개 계정도 팔로워를 노출한다는 사실을 반영해
  "형식 불일치" 와 "점수 반영 불가" 를 다른 문구로 분리했다.
- **CI-001 / P2-3** (`d624162`, `0302cdf`) — 축별 입력 결측을 채점 시점에 기록하고
  (`score_breakdown.missing_inputs`) readiness·Dashboard 에 노출한다. 결측 판정기를
  `scorer.missing_inputs_for_row` 하나로 통일했다.

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

1. **Stale lock 은 더 이상 수동 정리가 필요 없다 (절차 변경)** —
   `.browser_discovery.lock`(PID 12428)이 남아 있어도, 그 파일을 쥔 프로세스가 없으면
   다음 실행이 **TTL 과 무관하게 즉시 인수**한다(`12cde06`).
   **lock 파일을 지우지 마십시오** — 삭제는 inode 동일성을 깨서 두 실행이 동시에
   돌 수 있다. 멈춘 실행을 끝내려면 **그 PID 를 종료**한다. 종료 즉시 커널이 해제한다.
   (이전 안내 "이미 종료됐다면 lock 파일을 지우면 됩니다" 는 **무효**다.)
2. **커널 락 동작은 Windows 실기에서 미검증** — 개발 환경이 Linux 다. Windows 분기는
   가짜 `msvcrt` 주입으로만 검증했다. **운영 첫 실행 로그에서 `mode=KERNEL` 이
   찍히는지 확인**한다. `mode=TTL_FALLBACK reason=…` 이면 커널 판정이 비활성이고
   기존 TTL 동작으로 돌아간 것이다(안전한 방향이며 사유가 로그에 남는다).
   사후 확인 목록:
   - `mode=KERNEL` 인가, 아니면 `TTL_FALLBACK` 사유는 무엇인가
   - 예약 실행 계정과 수동 실행 계정 **둘 다** lock 파일을 열 수 있는가(`LockIOError` 없음)
   - 락 보유 중 차단 로그에 소유자 pid·생성 시각이 나오는가(안 나와도 판정은 정상)
   - `taskkill /F` 후 **즉시** 다음 실행이 lock 을 잡는가 (P1-1 수용 기준)
   - `data/` 가 OneDrive 동기화 폴더인가(그렇다면 1주간 `UNREADABLE` 빈도를 관찰)
3. **TTL 이 더 이상 살아 있는 소유자를 빼앗지 않는다 (동작 변화)** — 멈춘(hung)
   프로세스는 자동 복구되지 않고 운영자가 종료해야 한다. 반대로 합법적인 장시간
   실행(브라우저는 60분을 넘길 수 있다)을 TTL 이 중간에 빼앗던 위험은 사라졌다.
   TTL 초과 시 WARNING "비정상적으로 긴 실행 — 회수하지 않습니다" 가 매 실행 남는다.
4. **브라우저를 여는 4개 Runtime 은 서로 배타적이지 않다 (잔여 위험)** —
   lock 파일은 5개인데 브라우저 프로필(`data/browser_profile`)은 1개다.
   Discovery / Autopilot / COMMENT Canary / Canary Resume 가 서로 다른 lock 을 각자
   정당하게 잡고 같은 프로필을 열 수 있다. 완화 요인: chromium 자신의 프로필 락이
   두 번째 기동을 **실패로 드러내고**, 부모 없는 고아 chromium 은 스스로 Instagram 에
   쓰지 않는다. 프로필 단위 단일 락 또는 4개를 한 lock 으로 합치는 안은 **운영 동작을
   바꾸므로**(Discovery 가 Canary 를 막는다) 운영자 결정 영역이다 — 권고까지만.
5. **19N 팔로워 파싱은 실제 Instagram에서 미검증** — 개발 환경에서 instagram.com이
   차단되어 있어 로컬 DOM fixture로만 확인했다. 실행 로그가 이제 사유별로 나온다:
   `프로필 보강: 대상 Creator N · followers_public=… · parse_failed=… · …`
   `parse_failed` 가 3건 이상이고 공개·비공개 합쳐 확보가 0이면 **형식 불일치 WARNING**,
   비공개에서만 읽혔으면 **무효 WARNING**(그 후보는 `skip_private_accounts` 로 탈락).
6. **COMMENT Canary는 개발 환경에서 실행 불가** — instagram.com 차단 + 로그인 세션 없음.
   운영자 PC에서만 수행한다 (저장소 루트 `BLOCKED.md` 참조).
7. **사람 Feedback 표본 부족** — 임계값 재검토는 10건 이상 축적 후에만 근거가 생긴다.
   그 전에는 임계값을 건드리지 않는다.

## Last Regression

`python -m pytest targeting_agent/tests -q` → **917 passed** (2026-10-19, CI-001)

실제 Instagram / Claude CLI 호출 0, Task Scheduler 변경 0.
CI-001 에서 86건이 늘었다(831 → 917). 커널 락은 실제로 두 프로세스를 띄워
검증하고, Windows 분기는 주입으로 덮었다. 각 수정은 변이 검사로 실효성을 확인했다.

## Next Recommended Task

1. (운영자) `.\run_targeting_discovery.bat` 실행. **lock 파일을 미리 지우지 않는다** —
   소유자가 없으면 자동 인수된다. 로그에서 확인할 것:
   - `mode=KERNEL` (또는 `TTL_FALLBACK reason=…`) ← 커널 락 실동작 판정
   - `프로필 보강: 대상 Creator N · followers_public=… · parse_failed=… · …`
     그리고 형식 불일치 / 무효 WARNING 이 뜨는지
2. `--stats` 로 82점 이상 후보 확인 → `run_targeting_comment_canary.bat --rehearsal`.
3. Dashboard Stats 탭에서 `creator_fit 결측(browser)` 등 4개 타일과
   `결측기록 커버리지(browser)` 를 본다. 커버리지가 낮으면 숫자는 추정 기준이다.
4. 위 결과를 가지고 Analytics 읽기 전용 검토 (예시 C) → 개선 후보 Top 3.
5. 관찰 결과가 없으면 신규 Phase를 열지 않는다. 운영 근거 우선.
   **Next Review: 운영 데이터 축적 후** (실행 로그 + 결측 커버리지).

## Optional / 진행을 막지 않음

- Meta Graph API: 쓰기 미지원 확정. 해시태그 읽기만 가능(권한 심사 필요).
- Gemini 분석/댓글 생성 실사용 검증 (API Key 필요).
- 학습 임계값(`min_keyword_media` 등) 재조정은 운영 데이터 축적 후.
