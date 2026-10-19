# Continuous Improvement Ledger

실제 운영에서 드러난 문제와, **다음 작업에 재사용할 교훈**만 기록한다.
일시적 오타·lint·한 번뿐인 실수는 기록하지 않는다. 현재 상태는
`targeting_agent/WORK_STATE.md`에 있고, 이 파일은 장기 보존용이다.

각 항목 형식: Problem / Evidence / Root Cause / Change / Verification /
Regression Prevention / Reusable Lesson.

---

## 1. Browser Discovery가 LOCKED로 아무 것도 하지 않음

- **Area**: `scheduler/lock.py`, Browser Discovery runtime
- **Date**: 2026-10 (운영자 실제 실행)

**Problem**
Browser Discovery 실행 시 LOCKED 상태로 아무 작업도 하지 않음.
로그는 "다른 Scheduled Run이 진행 중입니다"라고 표시했지만, 운영자는
Scheduler를 실행하지 않았다. 운영자가 상황을 오해할 수 있는 상태였다.

**Evidence**
- lock 파일: `targeting_agent/data/.browser_discovery.lock`, PID 12428
- 운영 실행 결과: 상태 LOCKED · 세션 UNKNOWN · 검색어 0개 · 수집 0개
- 해당 PID는 이미 종료된 프로세스였다(stale lock).

**Root Cause**
`SchedulerLock` 구현을 여러 Runtime(Scheduler / Browser Discovery /
Autopilot / Canary)이 공유하면서, 로그의 owner label이 `Scheduled Run`으로
고정되어 있었다. 실제로 lock을 잡은 주체와 로그에 표시되는 주체가 달랐고,
stale lock 여부를 운영자가 판단할 정보(소유자·PID 생존 여부·복구 방법)가
로그에 없었다.

**Change**
commit `04b40ef` — `SchedulerLock`에 `label` 필드를 추가하고, 호출자가
자기 이름을 넘긴다: `label="Browser Discovery"`(browser_runner),
`label="Autopilot"`(autopilot/runner), `label="COMMENT Canary"`
(canary/comment_runner), 기본값 `"Scheduler Run"`.
차단 로그에 **어느 실행이 / 어떤 PID로 / 어느 파일을** 잡고 있는지와
복구 안내("이미 종료됐다면 lock 파일을 지우면 됩니다")를 함께 출력한다.
> ⚠️ 이 복구 안내는 **CI-001에서 무효가 됐다**(아래 Follow-up 2). lock 파일 삭제는
> inode 동일성을 깨서 2중 실행을 만든다. 현재 안내는 "프로세스를 종료하라"다.

**Verification**
commit `04b40ef` 전체 회귀: **830 passed**.
lock 동작(획득·차단·해제) 자체는 변경하지 않았고 메시지와 소유자 표기만 바뀌었다.

**Regression Prevention**
- lock 차단 경로는 label을 포함해 테스트한다.
- 새 Runtime이 lock을 사용하면 `label`을 반드시 넘긴다(기본값에 의존하지 않는다).
- `CLAUDE.md` §8 Lock 체크리스트: owner · PID · PID 생존 여부 · 경로 ·
  생성 시각 · stale timeout · 복구 안내.

**Follow-up (재검토에서 발견)**
`04b40ef` 이후 같은 결함이 남아 있는지 다시 확인했더니, lock 사용처 5곳 중
`canary/resume.py`가 `label`을 넘기지 않아 자기 lock 파일
(`data/.canary_resume.lock`)을 잡고도 로그에는 "다른 Scheduled Run이 진행 중"이라고
출력하고 있었다. 1번과 **동일한 결함이 누락된 Runtime 한 곳에 남아 있었다.**
`label="Canary Resume"`를 넘기도록 고치고, 개별 호출부를 믿지 않도록
**소스 전체를 훑어 lock 사용처마다 label을 검사하는** 회귀 테스트를 추가했다
(Scheduler만 기본값 허용). 사용처 수 하한도 함께 검사해 테스트가 탐색 범위를
좁게 잡는 것을 막는다.

**Follow-up 2 — CI-001: 판정을 커널에 맡겼다**
`04b40ef`는 로그 표기만 고쳤고, 죽은 소유자의 lock이 TTL(30/60분)까지 실행을
막는 문제는 남아 있었다. CI-001에서 그 판정을 커널 파일락(`fcntl.flock` /
`msvcrt`)으로 옮겼다(`12cde06`, `b5e9397`).

1차 설계는 PID 생존 조회(Windows ctypes `OpenProcess`)였고 **독립 Safety Reviewer가
기각했다**: ① `unlink` 후 재생성이 2중 실행을 막지 못한다(A가 unlink하고 새 lock을
만들면 B의 unlink가 A의 살아있는 lock을 지우고 통과 — 샌드박스에서 재현), ②
`OpenProcess` 성공률이 **측정되지 않은 전제**이고, 가장 위험한 조합(다른 계정으로
띄운 예약 실행 + 운영자 수동 실행)에서 효과가 0이다.

커널 락은 자기가 연 fd에 대한 연산이라 타 프로세스 조회 권한이 필요 없고,
성공 경로가 자기 검증적이다. 그 결과 PID 재사용 · 권한 오판 · TTL 만료 중 생존
탈취(시계 도약 포함) · corrupt 즉시 회수 · claim 경쟁이 **판정에서 사라졌다**.

**복구 안내가 역전됐다**: "lock 파일을 지우면 됩니다" → **"프로세스를 종료하면 즉시
해제됩니다. lock 파일을 지우지 마십시오(지우면 두 실행이 동시에 돌 수 있습니다)."**

**Reusable Lesson**
모든 Runtime lock은 일반적인 "lock exists"만 판단해서는 안 된다.
**누가 잡았는지 · 그 프로세스가 아직 살아 있는지 · 운영자가 어떻게
복구하는지**까지 알려주어야 한다. 공유 구현에 고정 문자열을 남기면
동작은 맞아도 운영자가 원인을 잘못 짚는다.
그리고 이런 결함은 **한 호출부만 고치면 나머지에 그대로 남는다** — 호출부를 하나씩
고친 뒤에는 "같은 패턴이 남은 곳이 없는지" 전수로 검사하는 테스트를 남긴다.

**CI-002 보강**: 복구 안내는 **층을 넘어가면 거짓이 된다.** Discovery 층이 만든
"로그인한 뒤 다시 시도하세요" 를 Canary 층이 그대로 전달했는데, Canary 는 종료 상태라
재로그인해도 영구히 NOOP 이었다. 재개 CLI 는 아예 없었다. 상류의 안내를 하류가
그대로 복사하지 않고, **그 층에서 실제로 통하는 수단**을 적는다. 그리고 안내대로 하면
정말 재개되는지를 **테스트로 고정**한다(그러면 안내와 수단이 갈라질 수 없다).

owner label이 정확한 것만으로는 **충분하지 않다**. stale 판정에는 소유자 생존의
**증거**가 필요하고, 그 증거는 우리가 추측하는 것(PID 조회·시간 경과)보다
**커널이 보증하는 것**(우리가 쥔 파일락)이 낫다. 불확실한 경우에는 회수하지 않는
방향으로 수렴시킨다. 그리고 상호배제 자원을 **삭제로** 해제하지 않는다 — 삭제는
식별자(inode) 동일성을 깨서 "둘 다 소유자" 상태를 만든다. 운영자에게 주는 복구
안내도 그 사실과 일치해야 한다.

---

## 2. 수집 단계의 silent skip이 결과를 0으로 만들었다

- **Area**: `discovery/browser_instagram.py`
- **Evidence**: commit `002ccb8` (Phase 18A.5). 운영 실행에서
  Found 10 · Collected 0 · errors 0 — 오류가 0인데 수집도 0이었다.
- **Root Cause**: 후보 변환 경로에 조건 미충족 시 `return None`으로 조용히
  빠지는 분기가 있었고, URL 패턴이 `/<username>/reel/<code>/` 형태를
  거부했다. 실패가 어디에서도 계측되지 않아 errors 0으로 보였다.
- **Change**: URL 패턴 수정, skip 사유별 카운터, `accounted()` 자체 점검
  (found = collected + skip 합계), 한도 있는 진단 로그, EMPTY 상태 구분.
- **Verification / Regression Prevention**: skip 사유별 테스트 + found/collected
  정합성 테스트. 이후 "오류 0 · 수집 0"은 EMPTY로 구분되어 드러난다.
- **Reusable Lesson**: **오류 0과 성공은 다르다.** 건너뛰기에는 반드시 사유
  카운터를 붙이고, 입력 수와 출력 수가 맞는지 스스로 검산하게 한다.
  계측되지 않은 `return None`은 버그가 아니라 버그 은폐 장치다.
- **CI-001 보강**: 검산을 베낄 때 **분모를 어디서 재는지**가 전부다. 이미 잘린
  루프 안에서 분모를 올리면 그 검산은 런타임에 깨질 수 없는 **항등식**이고,
  관용구의 모양만 남는다. 분모는 루프와 **독립적으로 상류에서** 재야 한다
  (18A.5가 유효했던 이유다). 또 **성공 분자가 쓸 수 있는 결과와 쓸 수 없는
  결과를 섞으면 실패가 숨는다** — 비공개 계정에서 읽은 팔로워는 하류에서 탈락해
  점수에 반영되지 않는데 "확보"로 세고 있었다. 지표는 "무엇을 셌는가"가 아니라
  **"그게 쓸 수 있는 것인가"**로 쪼갠다.

---

## 3. 선택자를 추측으로 고치면 같은 실패가 반복된다

- **Area**: `discovery/browser_instagram.py` (Instagram DOM)
- **Evidence**: commit `9cdba1f` (Phase 18A.6) — Found 0 → 수집 9/9.
  그 전 단계에서는 5개 검색어 전부 SELECTOR_MISMATCH / Timeout 20000ms였다.
- **Root Cause**: URL을 추정해 진입하는 경로와 영어 전용 선택자에 의존했다.
  화면이 달라지면 전체가 한꺼번에 실패했다.
- **Change**: 공개 UI 경로로 이동, 역할·라벨·href 기반의 단계별 선택자,
  단계마다 제한된 timeout, media_type은 게시물을 연 뒤에 판정, shortcode 기준 dedupe.
- **Verification / Regression Prevention**: 127.0.0.1에 실제 DOM fixture를
  띄워 실제 Chromium으로 검증한다(실제 Instagram 호출 없음).
- **Reusable Lesson**: DOM 실패는 **관찰된 구조**로만 고친다. 선택자를 바꿔보며
  재시도하지 않는다. 깨지기 쉬운 선택자(nth-child·클래스 해시·깊은 CSS)와
  단일 언어 가정은 쓰지 않고, 단계를 쪼개 어디서 끊겼는지 남긴다.

---

## 4. 같은 대상을 두 번 세는 집계는 숫자를 신뢰할 수 없게 만든다

- **Area**: `core/database.py`, `dashboard/queries.py`, `scheduler/summary.py`,
  `learning/metrics.py`
- **Evidence**: commit `e0856dc` (Phase 17.1) — 운영 화면에
  "Claude 분석 7 / Heuristic 7"이 동시에 표시됐다.
- **Root Cause**: Claude로 분석한 후보는 `media_analysis`에 heuristic 행과
  claude_code 행을 **둘 다** 갖는데, 집계가 행 수를 세고 있었다.
- **Change**: `count_by_final_analyzer()` / `final_analyzer_of()`로 후보별
  **최종** 분석 방식을 distinct 집계하고, 4개 지표 경로에 같은 기준을 적용했다.
  DB 데이터는 고치지 않고 집계 Query만 바꿨다.
- **Verification / Regression Prevention**: 분석 완료 = Claude + Heuristic 합이
  DB와 일치하는지 테스트한다. 지표를 추가할 때 "행 수"가 아니라 "대상 수"인지 확인한다.
- **CI-002 보강**: 새 이벤트를 기존 run 테이블에 넣으려다 그만뒀다. LOCKED 는 Run 이
  아니고(검색어 0·후보 0), 넣으면 집계 6곳의 단위가 바뀐다. 더 결정적인 것은 lock
  사용처 5곳 중 2곳에는 run 테이블이 **아예 없어서** 그 방식이 절반만 덮는 수단이라는
  점이었다(교훈 1번의 재발). **KPI 를 "질의 수정" 으로 지키지 말고 "그 테이블에 쓰지
  않음" 으로 지킨다** — 질의 6곳을 동시에 고치는 쪽은 한 곳만 빠뜨려도 숫자가 갈라진다.
  그리고 그 불변을 **"이벤트를 넣고 지표가 같은지" 비교하는 테스트**로 못박는다.
  그 테스트는 반드시 공허하지 않은지 확인한다(대조군으로 실제 행을 넣어 지표가 변하는지).
- **Reusable Lesson**: 지표는 **집계 단위**를 먼저 정한다(행이 아니라 후보).
  같은 수치를 여러 화면이 각자 계산하면 반드시 갈라진다 — 기준 함수를 하나 두고 공유한다.
  Claude **호출 수**와 Claude **최종 분석 수**처럼 뜻이 다른 값은 합치지 않는다.

---

## 5. 실행 조건은 한 곳에서 완전하게 정의한다

- **Area**: `core/database.py`, `actions/controller.py`
- **Evidence**: commit `cdcd266` (Phase 14.1) — 실행 대상 조회가
  `status='PENDING'`만 조건으로 삼아, 파이프라인이 자동 생성한 **미승인**
  Action이 Executor 실행 대상에 포함됐다.
- **Root Cause**: 승인 여부가 조회 조건에서 빠져 있었다. 상태 enum만 보면
  승인된 건과 자동 생성된 건이 구분되지 않았다.
- **Change**: `fetch_pending_actions` → `fetch_executable_actions`,
  조건을 `status='PENDING' AND approved_at IS NOT NULL`로 강화.
  상태 enum은 늘리지 않고 `approved_at` NULL 여부로만 구분한다.
  승인은 기존 Safety 조건(일일 한도·creator cooldown·중복 Interaction)을
  **대체하지 않고 추가된다**.
- **Verification / Regression Prevention**: E2E를 "승인 전 실행 0건 →
  Dashboard 승인 → 실행 1건"으로 갱신했다.
- **Reusable Lesson**: 실제 행동(Instagram 쓰기)으로 이어지는 조건은
  **한 함수에서 전부** 판정하게 하고, 이름이 그 뜻을 담게 한다(`executable`).
  안전 조건을 추가할 때 기존 조건을 대체하지 않는지 확인한다.

---

## 6. 지표가 한 값에 고정되어 있으면 임계값이 아니라 입력을 본다

- **Area**: `discovery/browser_profile_meta.py`, `discovery/browser_instagram.py`
- **Evidence**: commit `31b16d9` (19N). COMMENT Canary가 항상 NO_CANDIDATES였고,
  18D.2 보정 결과 브라우저 후보 점수가 79.5에서 멈췄다(임계값 82).
  `creator_fit`이 언제나 중립값 0.5였다 — 팔로워 수를 한 번도 읽지 않았다.
- **Root Cause**: 6축 중 한 축의 입력이 결측이면 중립 0.5가 들어가고, 그 상태로는
  구조적으로 임계값을 넘을 수 없었다. 임계값이 높은 것이 아니라 입력이 없었다.
- **Change**: 프로필 `og:description` **문자열 파싱**으로 팔로워 규모를 읽고
  (네트워크 가로채기 없음), creator 당 1회·실행당 상한·실패는 격리.
  읽지 못하면 `None`(0이 아니다). 점수 79.5 → 89.5. **임계값은 그대로 두었다.**
- **Verification / Regression Prevention**: 파서 단위 테스트(정수·`15K`·`1.2M`·
  비공개·읽기 실패), 보강 실패가 수집을 깨지 않는지 확인.
- **Reusable Lesson**: 결과가 안 나올 때 **임계값을 내리는 것은 마지막 수단이고,
  운영자 결정 영역이다.** 먼저 점수 구성 축마다 실제 입력이 들어오는지 확인한다.
  결측은 `0`이 아니라 중립값/`None`으로 두고, 중립값이 계속 보이면 그것을
  "데이터 없음" 신호로 읽는다.
- **CI-002 보강**: 교훈 6번이 걱정한 "임계값을 내리자" 압력의 **가장 흔한 발생원은
  상류 운영 장애가 점수 부족으로 보이는 것**이었다. Canary Resume 이 Discovery 의
  LOCKED·세션 중단·실패를 모두 "임계값 이상 후보 없음(최고점 N)" 으로 기록했고, 그
  최고점마저 이번 실행이 아니라 DB 전체 누적이었다. 같은 오해가 readiness 보고서에도
  복사돼 있었다(`"LIKE 임계값 / 실제 최고점": "75 / 92.40"`). **임계값 옆에 숫자를
  놓을 때는 그 숫자가 무엇의 집계인지 라벨에 적는다.**
- **CI-001 보강**: 중립값 fallback은 **낮은 점수와 구분해 관측**해야 한다. 그리고
  그 관측의 **표본이 측정 대상에 의해 편향되지 않는지** 반드시 따진다 — "기록이
  있는 행만 센다"가 보수적으로 보였지만, 기록은 재채점에서만 생기고 재채점은
  점수 상위순이라 **결측 행은 점수가 낮아 구조적으로 표본에서 빠졌다**. 결측이
  심할 때 정확히 침묵하는 지표였다. 표본을 좁힐 때는 "좁힌 기준이 측정하려는
  현상과 상관되어 있는가"를 먼저 묻고, 분모와 신뢰도(커버리지)를 함께 노출한다.

---

## 7. 검증되지 않은 설계 전제가 결함의 원인이었다 (CI-001)

- **Area**: 개선 사이클 전반 (설계 단계)
- **Evidence**: CI-001 에서 **같은 종류의 실패가 세 번** 나왔다. 전부 구현이 아니라
  **전제**가 틀렸고, 전부 독립 Reviewer 가 잡았다.
  1. "비공개 계정은 팔로워를 노출하지 않는다" → 틀렸다. 파서로 재현:
     `parse_profile_meta(og="1,234 Followers…", body="비공개 계정입니다")`
     → `is_private=True, followers=1234`. 이 전제 위에 세운 전멸 경고가
     비공개 1건만 섞여도 무력화됐다 (`fb032e5`).
  2. "Windows `OpenProcess(SYNCHRONIZE)` 는 보통 성공한다" → **측정된 적이 없다.**
     예약 실행이 다른 계정·세션이면 ACCESS_DENIED 로 떨어져, 2중 실행 위험이
     가장 큰 조합에서 효과가 0 이다. 개발 환경이 Linux 라 측정 자체가 불가능했다.
     설계를 폐기하고 커널 락으로 전환했다 (`12cde06`).
  3. "경고 표본을 기록된 행으로 좁히면 보수적이다" → 편향이었다. 기록은 재채점에서만
     생기고 재채점은 점수 상위순이므로 결측 행이 구조적으로 빠진다 (`0302cdf`).
- **Root Cause**: 세 경우 모두 **그럴듯해서 확인하지 않았다.** 코드로 확인하는 데
  각각 몇 분이면 충분했다(파서 1회 호출 / 호출부 읽기 / 재채점 쿼리의 ORDER BY 읽기).
- **Change**: 설계 단계에서 전제를 **실행 가능한 형태로** 확인하게 했다.
  파서 전제는 파서를 직접 호출해서, 샘플링 전제는 그 샘플을 만드는 쿼리를 읽어서,
  OS 전제는 운영에서 측정할 수 있는 로그(`mode=KERNEL` / `mode=TTL_FALLBACK reason=…`)를
  남겨서 확인한다. 마지막이 중요하다 — **측정할 수 없는 전제는 측정 가능하게 만든다.**
- **Verification**: 세 건 모두 변이 검사로 회귀를 고정했다. CI-001 전체 회귀 925 passed.
- **Regression Prevention**: 설계서에 "이 전제를 무엇으로 확인했는가" 를 전제마다
  한 줄로 남긴다. 확인할 수 없으면 **그 사실을 설계에 적고**, 그 전제에 의존하는
  분량을 줄이거나 측정 수단을 함께 만든다.
- **Reusable Lesson**
  구현 결함보다 **전제 결함이 비싸다** — 구현은 테스트가 잡지만 전제는 테스트도 같이
  속는다(테스트가 구현을 베끼면 틀린 전제를 함께 고정한다). 그럴듯한 전제를 만나면
  "이걸 지금 **코드로** 확인하는 비용이 얼마인가" 를 먼저 묻는다. 대개 몇 분이다.
  그리고 **이 환경에서 측정할 수 없는 전제**(운영 OS 동작 등)에 설계를 올리지 않는다.
  올려야 한다면 첫 운영 실행에서 판정이 나오는 **로그 한 줄**을 함께 만든다.

---

## 8. 상류의 실패 상태를 하류의 도메인 결과로 바꾸지 않는다 (CI-002)

- **Area**: `canary/resume.py`, `scheduler/runtime_state.py`, `actions/activity.py`
- **Evidence**: 세 경로에서 같은 종류의 번역이 일어났다.
  1. Discovery 가 `LOCKED`/`SESSION_STOPPED`/`FAILED` 를 돌려줬는데 Resume 이
     `(added, analyzed)` 로 축소해 상태를 버리고, 전부 `WAITING_ELIGIBLE`
     "임계값 이상인 실제 후보가 없다" 로 기록했다. **운영 장애가 점수 부족이 됐다.**
  2. `controller` 가 `platform_warning:CHALLENGE:…` 로 감싼 사유를 `split(":",1)[0]`
     이 읽어 **래퍼 라벨**을 신호로 썼다. `CHALLENGE`(STOP)가 `PLATFORM_WARNING`
     (CAUTION)으로 강등돼 활동이 멈추지 않았다.
  3. `_stop_status_for` 의 기본값이 `STOPPED_WARNING` 이라, **인식하지 못한 사유가
     특정 신호처럼** 기록됐다(로그인 만료가 "경고 종료"로 남았다). 그것을
     `STOPPED_SAFETY` 로 바꾸니 이번엔 **인식한 내부 사유가 "인식하지 못한" 으로**
     로그에 찍혔다 — 같은 거짓 신호가 반대 방향으로 재발했다.
- **Root Cause**: 층 경계에서 **표현을 바꿀 때 정보가 줄어드는 쪽으로만** 바꿨다.
  tuple 축소, 첫 토큰 추출, 기본값 fallback — 셋 다 "그 자리에 넣을 값" 을 만들려고
  원본을 버렸다.
- **Change**: 상태를 보존해 전달하고(`source_status`/`source_stop_reason`), 신호 추출을
  한 함수로 모아 **가장 심각한 신호**를 찾고, 인식 실패는 그 사실로 드러낸다.
  분류 기준이 여러 곳에 복제돼 있으면 하나로 합친다.
- **Verification**: 상태별 fixture 를 Verifier 가 **직접** 돌려 표로 확인했다 —
  LOCKED → `NOOP`/`WAITING_NEXT_RUN`(exit 0), FAILED → `BLOCKED`(exit 1),
  SESSION_STOPPED → terminal `STOPPED_*`, EMPTY/SUCCESS(0건)만 `WAITING_ELIGIBLE`.
  CI-002 전체 회귀 1062 passed.
- **Regression Prevention**: 상류 상태를 하류 상태로 매핑하는 자리에는 **매핑 표 전체를
  덮는 테스트**를 둔다(한 분기만 고정하면 나머지가 흐른다). 신호 문자열은 **토큰화한
  조각 전체가 신호 이름과 같을 때만** 신호로 본다 — substring 탐색은
  `"session ok, challenge=false"` 를 CHALLENGE 로 읽는다. 사람만 해제할 수 있는
  상태(STOPPED)로 올리는 판정에서는 특히 그렇다.
- **Reusable Lesson**
  **상류의 실패·차단 상태를 하류의 도메인 결과로 번역하지 않는다.** "lock 때문에 못
  돌았다" 를 "후보가 없다" 로, "세션이 끊겼다" 를 "점수가 모자라다" 로 바꾸면, 운영자는
  없는 문제를 고치려 들고(임계값 하향) 있는 문제는 보지 못한다. 층을 넘길 때는 **원본
  상태를 함께 실어 보내고**, 값을 만들어 채우기보다 **모른다는 사실을 드러낸다.**

  그리고 **경보가 정상 동작에서 뜨면 운영자는 그 경보를 무시하는 법을 배운다.**
  연속 차단 경고를 넣었더니 소유자가 정상적으로 길게 도는 동안에도 떠서 readiness 가
  CONDITIONAL_GO 로 내려갔다(게다가 소멸하지 않아 영구히 남았다). 경보는 "정상인데
  시끄러운" 경우를 먼저 제거해야 신뢰를 얻는다 — 정상 진행 중에는 보류하고, 사실은
  행에 남기고, 멈춘 소유자는 별도 신호로 잡는다.

---

## 정정 기록 (CI-002)

- **`CandidateIngestor` 가 commit 을 호출하지 않는다는 판단은 틀렸다.**
  `grep commit` 이 `discovery/ingest.py` 에서 0건이어서 그렇게 보였지만,
  `:171`·`:223` 의 `with transaction(self.conn)` 이 행마다 commit 한다
  (`core/database.py:98-105`). inbox 파일 이동은 그 commit 들이 끝난 뒤다.
  **후보 영구 유실은 없다.**
  이 잘못된 사실이 커밋 `bffe3c2` 의 메시지에서 "scheduler 경로의 새 커밋은 개선이다"
  의 근거로 쓰였다. 정확한 상태는 **중립 내지 약한 비대칭**이다 — 예외로 끝난 Run 의
  부분 쓰기가 영속되는데 `scheduled_runs` 행은 없다. 그 비대칭을 고치는 것이 다음
  사이클 후보다.
  **교훈**: `grep` 으로 "호출이 없다" 를 단정하지 않는다. 헬퍼·context manager 에
  위임된 호출은 grep 에 걸리지 않는다. 호출 경로를 따라가 확인한다.
