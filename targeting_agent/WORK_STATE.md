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

**Last Improvement Cycle: CI-002** (Runtime State Integrity & Observability)
완료: P1-A (Discovery 상태 보존) · P1-B (Runtime 이벤트 관측 + 플랫폼 신호 파싱)
P0-Research (공유 Playwright 프로필 실측 — 구조 변경 없음)
이전: CI-001 (lock 생존 판정 · enrichment 관측성 · score 입력 결측 관측성)

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
- **CI-002 / P1-A** (`34c69ef`, `cc07354`, `88e0c57`) — Canary Resume 이 Discovery 의
  `status`/`stop_reason` 을 버려 LOCKED·세션 중단·실패를 모두 "임계값 이상 후보 없음"
  으로 기록하던 문제. 상태별 분기(LOCKED→`WAITING_NEXT_RUN`, SESSION_STOPPED→terminal
  `STOPPED_*`, FAILED→`BLOCKED`), `source_status`/`source_stop_reason` 보존,
  종료 상태의 복구 안내를 실제 해제 수단과 맞춤, `_stop_status_for` 기본값을
  `STOPPED_WARNING`→`STOPPED_SAFETY`.
- **CI-002 / P1-B** (`99540ac`, `bffe3c2`, `609ab5e`) — LOCKED·멈춘 소유자·Canary 정지가
  DB 에 남지 않던 문제. `scheduler/runtime_state.py` 와 `app_state` 키 2종으로 기록하고
  readiness·Dashboard 에 노출한다. **집계 질의를 하나도 고치지 않았다.**
  플랫폼 신호 파싱이 래퍼 라벨 때문에 CHALLENGE 를 CAUTION 으로 강등하던 결함도 고쳐
  `actions/activity.py` 의 `extract_signal` 하나로 모았다.

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
   정당하게 잡고 같은 프로필을 열 수 있다.
   **CI-002 에서 Linux 로 실측했다**(임시 프로필, instagram.com 접속 없음):
   정상 종료 후 동시 기동은 `CHROMIUM_BLOCKS` — Chromium 의 ProcessSingleton 이 두 번째를
   막는다(`exitCode=21`, 0.7s, 타임아웃 아님). SIGKILL 후에는 `SAFE_ISOLATED` — 남은
   `SingletonLock` 이 죽은 pid 를 가리키면 Chromium 이 무시하고 진행한다(수동 삭제 불필요).
   `POTENTIAL_CONFLICT`(둘 다 열려 동시에 씀)는 **관찰되지 않았다.**
   그러나 **저장소 코드는 이 보호에 의존하지도 확인하지도 않고** 두 번째 launch 실패를
   일반 예외로만 처리한다. **Windows 실측과 headed 모드는 UNVERIFIED** 다.
   프로필 단위 단일 락 또는 4개를 한 lock 으로 합치는 안은 **운영 동작을 바꾸므로**
   (Discovery 가 Canary 를 막는다) 운영자 결정 영역이다 — 권고까지만.
5. **19N 팔로워 파싱은 실제 Instagram에서 미검증** — 개발 환경에서 instagram.com이
   차단되어 있어 로컬 DOM fixture로만 확인했다. 실행 로그가 이제 사유별로 나온다:
   `프로필 보강: 대상 Creator N · followers_public=… · parse_failed=… · …`
   `parse_failed` 가 3건 이상이고 공개·비공개 합쳐 확보가 0이면 **형식 불일치 WARNING**,
   비공개에서만 읽혔으면 **무효 WARNING**(그 후보는 `skip_private_accounts` 로 탈락).
6. **COMMENT Canary는 개발 환경에서 실행 불가** — instagram.com 차단 + 로그인 세션 없음.
   운영자 PC에서만 수행한다 (저장소 루트 `BLOCKED.md` 참조).
7. **사람 Feedback 표본 부족** — 임계값 재검토는 10건 이상 축적 후에만 근거가 생긴다.
   그 전에는 임계값을 건드리지 않는다.
8. **Canary Resume 의 lock 경쟁은 관측되지 않는다 (CI-002 의 알려진 공백)** —
   Runtime 차단·미종료 기록은 scheduler · browser_discovery · autopilot · comment_canary
   **4개 런타임뿐**이다. `canary/resume.py` 는 `.canary_resume.lock` 을 쓰지만 기록을
   호출하지 않는다(두 Task 가 동시에 작업해 파일 교집합을 피한 결과다).
   `tests/test_runtime_events.py` 의 전수 검사가 이 미적용을 집합으로 고정해 두었으므로,
   적용하면 그 단정이 깨지며 후속 처리가 강제된다.
9. **동작 변화 — `--canary-resume` 의 exit code** — Discovery 가 실패하거나 세션이
   중단되면 이제 **exit 1** 이다(이전에는 "후보 없음" 으로 읽혀 exit 0). 교훈 2번대로
   실패가 exit 0 이면 안 되지만, Task Scheduler 의 "마지막 결과" 표시가 달라진다.
   `install_canary_scheduler.ps1` 에 `-RestartCount` 가 없어 **재시도를 유발하지 않는다**
   (확인했다). LOCKED 는 여전히 exit 0 이다(lock 경쟁은 오류가 아니다).
10. **동작 변화 — Scheduler 의 부분 쓰기** — `finally` 의 `record_finished` 가 호출자
   conn 을 commit 하므로, 예외로 끝난 Run 의 부분 쓰기가 이전에는 `close` 때 롤백됐지만
   이제 영속된다. **정정**: 커밋 `bffe3c2` 의 메시지는 이것을 "개선" 이라고 적었고 그
   근거로 "`CandidateIngestor` 가 commit 을 호출하지 않는다" 를 들었는데 **그 근거는
   틀렸다**(ledger 정정 기록 참조). 정확한 상태는 **중립 내지 약한 비대칭**이다 —
   부분 쓰기는 남는데 `scheduled_runs` 행은 없다. 그 비대칭을 고치는 것이 다음 사이클
   후보 2번이다. `comment_canary` 는 이 경로를 `rollback` 으로 막아 두었다.
11. **동작 변화 — 플랫폼 신호 분류** — `extract_signal` 토큰화로 `CHALLENGE` 가 래퍼
   라벨에 가려지지 않는다(CAUTION → **STOPPED**, 의도된 수정). 부수적으로
   `"CHALLENGE 아님: platform_warning"` 같은 문자열이 NORMAL → **CAUTION** 으로 한 단계
   올라간다. 조각이 정확히 `PLATFORM_WARNING` 이므로 설계대로이고 활동량 감소 방향이다.

## Last Regression

`python -m pytest targeting_agent/tests -q` → **1062 passed** (2026-10-19, CI-002)

CI-001 에서 831 → 925, CI-002 에서 925 → 1062. 기준선 831 은 `8a77ae5` 시점이고
ledger 1번이 적은 830 은 그 앞 `04b40ef` 시점이며 둘은 모순이 아니다.

Verifier 가 독립 검증했다(CI-002). `socket.connect`/`getaddrinfo`/`subprocess.Popen` 을
후킹해 전체 회귀를 재실행한 결과 **네트워크 connect 0건 · DNS 조회 0건 ·
`claude` 실행 0건** 이다. `Popen` 26건은 lock 테스트가 띄운 자식 프로세스 19건과
기존 Playwright 드라이버 7건(대상은 127.0.0.1 fixture 서버)이다.
KPI 불변은 Verifier 가 자기 코드로 대조군까지 세워 확인했다 — `app_state` 에
`runtime_run:*`/`runtime_block:*` 를 넣어도 4개 지표와 19개 테이블이 동일하고,
대조군으로 실제 run 행을 넣으면 지표가 변한다(비교가 공허하지 않다).
Runtime 격리: `config.yaml`·`core/schema.py`·`core/database.py`·`scripts/*`·`*.bat`·
`*.ps1`·requirements diff **0바이트**, 기존 숫자 리터럴 변경 **0건**.

남은 UNVERIFIED: Chromium 이 자기 프로세스에서 내는 소켓은 Python 후크로 관측할 수
없다. **Windows 커널 락(`msvcrt`)은 Linux 에서 실행할 수 없다** — 운영 첫 실행 로그의
`mode=KERNEL` 또는 `runtime_run:*` 의 `lock_mode` 로 확인한다.

## Next Improvement Candidates (CI-002 종료 후 읽기 전용 재평가)

판정 **BUG_FIX_REQUIRED**. 운영 지표는 INSUFFICIENT_DATA(컨테이너 DB 는 개발 fixture).
코드 근거 기반이며 **이번 사이클에서 고치지 않았다.**

1. **P0 — `--live-canary` 에 lock 이 없고, 브라우저 기동 예외가 Canary 를 영구 종료시킨다**
   `canary/runner.py` 에 `SchedulerLock` 호출이 없다(lock 사용처 5곳에서 LIKE Canary 가
   빠져 있다). 게다가 `launch_persistent_context` 예외가 `canary/runner.py:340-346` 의
   `except Exception` → `stop(FAILED, …)` 로 가고 `FAILED` 는 TERMINAL 이라,
   **첫 쓰기 전인데도 48시간 창이 소멸하고 Canary 가 영구 종료된다.** 같은 상황에서
   `canary/comment_runner.py:282-284` 는 Canary 를 끝내지 않는다 — 두 Canary 의 처리가
   다르다. `live_override` 가 DRY_RUN 설정을 메모리에서 LIVE 로 바꾸므로 **현재 설정에서도
   도달 가능**하다(운영자 수동 실행 또는 공유 프로필 충돌).
   범위: `canary/runner.py` · `canary/state.py` · `scheduler/runtime_state.py` + 테스트.
   lock 추가는 가드 강화라 자동 수정 범위다. 다만 **종료 상태의 의미를 바꾸는 쪽**
   (쓰기 전 실패를 비종료로)은 Reviewer·운영자 확인을 받아야 한다.
   `scripts/init_instagram_session.py` 도 lock 이 없다 — 별도 판단.
2. **P1 — `scheduled_runs` 가 예외로 끝난 Run 을 기록하지 않아 Scheduler 실패 KPI 가
   실패를 세지 못한다**
   `scheduler/runner.py:157` 의 `_record_run` 이 `try` 마지막 줄이고 `except`/`finally` 에는
   기록이 없다. 그래서 `learning/metrics.py:173-176` 의 `status='FAILED'` 조건이
   **도달 불가**다(`STATUS_FAILED` 는 `except` 에서만 설정되는데 그 뒤로 INSERT 가 없다).
   KPI 는 `0 / N` 으로 정상처럼 보인다. 위 알려진 이슈 10번의 비대칭이 바로 이것이다.
   범위: `scheduler/runner.py` + 테스트. schema 변경 불필요(컬럼이 이미 있다).
   고치면 `Scheduler 실패 X/N` 의 의미가 바뀌어 과거 값과 불연속이 생긴다 —
   **운영자에게 알리고 소급 보정은 하지 않는다.**
3. **P2 — `validate_session()` 이 bool 만 돌려줘 로그인 만료와 Challenge 를 구분하지 못한다**
   `actions/executors/browser.py:89-104`. 내부의 세션 상태와 `stop_reason()` 이 로그에만
   남아, `actions/controller.py:92` 의 사유가 `executor_session_invalid:<name>` 하나다.
   CI-002 는 원인을 추측하지 않고 `STOPPED_SAFETY` 로 멈추는 쪽을 택했다(안전 방향).
   정확히 기록하려면 반환 계약이 아니라 **마지막 세션 상태를 읽는 속성**을 executor 에
   두고 호출자가 읽게 하는 편이 bool 계약을 지킨다.
   범위: `actions/executors/browser.py`(**공유 파일 → 순차**) · `canary/runner.py`.
   후보 1번과 파일이 겹치므로 **1번이 먼저다.**

보류·제외:
- **`CandidateIngestor` 가 commit 을 안 한다는 후보는 반증됐다** — `with transaction()` 이
  행마다 commit 한다. ledger 정정 기록 참조.
- 알려진 Discovery status 집합이 `canary/resume.py` 에 복사된 것은 **P3** 다 — 경고가
  뜨므로 조용한 drift 가 아니다. `browser_runner` 로 옮겨 import 하는 통합은 선택.
- `--canary-reset` CLI 는 **Safety Gate 완화이므로 운영자 결정 영역** — 권고까지만.
  후보 1번(b)이 적용되면 필요도가 줄어든다.
- 통합 Global Browser Lock 도 **운영자 결정 영역**(Discovery 가 Canary 를 막는다).

## Optional / 진행을 막지 않음

- Meta Graph API: 쓰기 미지원 확정. 해시태그 읽기만 가능(권한 심사 필요).
- Gemini 분석/댓글 생성 실사용 검증 (API Key 필요).
- 학습 임계값(`min_keyword_media` 등) 재조정은 운영 데이터 축적 후.
