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

**Verification**
commit `04b40ef` 전체 회귀: **830 passed**.
lock 동작(획득·차단·해제) 자체는 변경하지 않았고 메시지와 소유자 표기만 바뀌었다.

**Regression Prevention**
- lock 차단 경로는 label을 포함해 테스트한다.
- 새 Runtime이 lock을 사용하면 `label`을 반드시 넘긴다(기본값에 의존하지 않는다).
- `CLAUDE.md` §8 Lock 체크리스트: owner · PID · PID 생존 여부 · 경로 ·
  생성 시각 · stale timeout · 복구 안내.

**Reusable Lesson**
모든 Runtime lock은 일반적인 "lock exists"만 판단해서는 안 된다.
**누가 잡았는지 · 그 프로세스가 아직 살아 있는지 · 운영자가 어떻게
복구하는지**까지 알려주어야 한다. 공유 구현에 고정 문자열을 남기면
동작은 맞아도 운영자가 원인을 잘못 짚는다.

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
