# 지속 개선 체계 사용법

## 1. 목적

이 프로젝트의 모든 작업을 다음 흐름으로 고정한다.

```
실제 운영 결과 → 문제 탐지 → 원인 분석 → 개선 설계 → 구현
→ 독립 검토 → 검증 → 회고 → 프로젝트 메모리 축적 → 다음 작업에 자동 반영
```

목표는 **같은 실패를 두 번 겪지 않는 것**이고, 한 번에 많이 만드는 것이 아니다.
운영 중인 Instagram 자동화라서, 속도보다 안전과 재현성이 우선이다.

> OMC(oh-my-claudecode) 플러그인은 이 환경에 설치되어 있지 않다. 같은 역할을
> Claude Code 기본 기능(project memory + `.claude/agents/` + Agent 호출)으로 구성했다.
> 나중에 OMC를 설치하더라도 이 파일들을 덮어쓰지 않는다.

## 2. 구성 파일

| 파일 | 역할 |
| --- | --- |
| `CLAUDE.md` (저장소 루트) | 세션 시작 시 자동 로드되는 운영 규칙 |
| `.claude/agents/*.md` | 8개 역할 정의 + 모델 라우팅 |
| `targeting_agent/docs/continuous_improvement.md` | 재사용 교훈 (장기 보존) |
| `targeting_agent/docs/omc_continuous_improvement.md` | 이 문서 (사용법) |
| `targeting_agent/WORK_STATE.md` | 현재 상태만 |

## 3. 역할과 모델 라우팅

| 역할 | 모델 | 쓰기 권한 | 책임 |
| --- | --- | --- | --- |
| **Coordinator** | 메인 세션 | git | 분해·순서·병렬 가능 여부·merge·commit |
| Explorer | haiku | 없음 | 증거 수집, 심볼·호출부 탐색 |
| Architect | opus | 없음 | 구조 영향·재발 위험·최소 설계 |
| Executor | sonnet | Edit/Write | 최소 diff 구현 |
| QA | sonnet | Edit/Write | 재현 테스트·회귀 |
| Reviewer | opus | 없음 | 작성자와 분리된 검토 |
| Verifier | sonnet | 없음 | 완료 주장 검증 |
| Analytics | sonnet | 없음 | 운영 지표 해석 |

비용 원칙: **탐색은 haiku, 구현·테스트는 sonnet, 설계·검토는 opus.**
단순 수정에 opus를 쓰지 않고, 안전·구조 판단을 haiku에 맡기지 않는다.

## 4. 작성자 ≠ 검토자 ≠ 검증자

구현한 Agent가 자기 작업을 최종 승인하지 않는다.

```
Executor(구현) → Reviewer(diff 검토, opus) → Verifier(명령 재실행, 증거 확인)
```

- Reviewer는 **작성자의 요약이 아니라 diff를 읽는다.**
- Verifier는 테스트를 **직접 다시 돌린다.** 확인할 수 없으면 `UNVERIFIED`로 표시한다.
- Reviewer 미해결 지적이 남아 있으면 완료가 아니다.

## 5. Memory 정책

- 새 작업을 시작하면 **그 영역 키워드로만** 교훈을 찾는다. 전체를 읽지 않는다.
  ```bash
  grep -n "lock" targeting_agent/docs/continuous_improvement.md
  ```
- 관련 없는 교훈을 끌어오지 않는다(lock 작업에 comment 품질 교훈을 주입하지 않는다).
- 기록 대상: 재발 가능한 원인, 잘못된 가정, 운영자 혼동을 유발한 표기, 구조적 한계.
- 기록하지 않음: 일시적 오타, lint, 한 번뿐인 실수.
- `WORK_STATE.md`는 **현재 상태만** 둔다. 과거 Phase 서술을 쌓지 않는다.
  둘에 같은 내용을 중복 기록하지 않는다.

## 6. 병렬 정책

병렬은 **파일 교집합이 없을 때만** 쓴다. 다음은 순차로 다룬다.

`core/schema.py` · `core/database.py` · `config.yaml` · `actions/policy.py` ·
`actions/executors/*` · `discovery/browser_*`

Instagram Web에 실제로 쓰는 경로는 항상 하나다(Runtime Orchestrator →
Safety Gate → Browser Lock → Browser Executor). 개발용 Agent는 브라우저를 열지 않는다.

## 7. 안전 경계

자동 수정 가능: 버그, 통계 오류, 로그 오류, silent failure, stale lock,
fixture/test 문제, Dashboard 표시 오류.

**권고까지만** (자동 적용 금지): LIKE/COMMENT 임계값 하향, 일일 한도 증가,
creator 한도 완화, cooldown 축소, LIVE 전환, COMMENT 공개 LIVE 활성화,
Safety Gate 약화, 플랫폼 경고 무시.

학습 금지: 탐지 회피, 사람 행동 위장, fingerprint/proxy/UA rotation,
rate-limit 탐색, Challenge 우회, 비공식 API·GraphQL 역분석, 응답 가로채기.
플랫폼 이상 신호에는 **활동량 감소 또는 STOP만** 허용한다.

---

## 사용 예시

### 예시 A — 운영 장애 Hotfix (단일 모듈)

> 상황: 운영자가 "Browser Discovery가 LOCKED만 뜨고 아무 것도 안 한다"고 보고.

1. **Recall** — `grep -n "lock" targeting_agent/docs/continuous_improvement.md`
   → 1번 항목(이 장애의 원본)과 §8 lock 체크리스트가 나온다.
2. **Explorer (haiku)** — `SchedulerLock` 정의와 호출부, lock 파일 경로를 `파일:줄`로 수집.
3. **Architect (opus)** — 원인이 공유 구현의 고정 label인지, lock 수명 정책인지 판정.
   Instagram 실행량에 영향이 있는지 확인(이 경우 없음 → 자동 수정 가능).
4. **Executor (sonnet)** — 최소 diff. 운영 임계값·한도·executor mode는 건드리지 않는다.
5. **QA (sonnet)** — 차단 경로 테스트를 **먼저 실패시키고** 통과시킨다.
6. **Reviewer (opus)** → **Verifier (sonnet)** — diff 검토, 테스트 재실행.
7. **Coordinator** — 커밋 1개 + 교훈 기록(재발 가능 원인이므로 ledger에 추가).

### 예시 B — 다중 모듈 기능 추가 (병렬)

> 상황: 새 Discovery 신호를 추가하고 Dashboard에 노출한다.

1. **Architect (opus)** — 파일 영향 범위를 먼저 그린다. 공유 파일
   (`schema.py`/`database.py`/`config.yaml`)은 **먼저 순차로** 처리한다.
2. 공유 파일 변경 완료 후, 교집합 없는 작업만 병렬:
   - Executor #1 — `discovery/` 신호 계산
   - Executor #2 — `dashboard/` 표시
   - QA — 테스트 (각 Executor의 파일과 겹치지 않게 테스트 파일 분리)
3. 같은 파일을 두 Agent가 동시에 고치려 하면 **병렬을 포기하고 순차로 바꾼다.**
4. **Reviewer (opus)** — 통합 diff 하나를 본다(각자 요약이 아니라).
5. **Verifier** — 모듈 테스트 → 전체 회귀. baseline을 과거 숫자에 맞추지 않고
   현재 값을 직접 확인한다.
6. 교훈은 "재사용 가능한 것"만 기록한다. 기능 설명은 Phase 문서에 둔다.

### 예시 C — 개선 검토 (읽기 전용)

> 상황: 운영 1주 후 "뭘 개선하면 좋을지" 점검.

1. **Analytics (sonnet, 읽기 전용)** — 기존 수집기·통계 함수를 재사용한다.
   DB에 쓰지 않는다. 표본이 짧으면 `INSUFFICIENT_DATA`, `None`은 `0%`가 아니라 `-`.
2. 결론은 다음 중 하나:
   `NO_CHANGE` / `BUG_FIX_REQUIRED` / `EXPERIMENT_RECOMMENDED` /
   `CONFIG_RECOMMENDATION` / `ARCHITECTURE_REVIEW_REQUIRED` / `SAFETY_STOP`
3. **코드를 자동 수정하지 않는다.** 개선 후보 Top 3까지만 만들고 우선순위를 붙인다.
   ```
   P0 Runtime 안정성 / 안전
   P1 운영 실패 (실행했는데 결과가 안 나오는 문제)
   P2 품질 (후보 품질·점수 정확도)
   P3 편의 (표시·UX)
   ```
4. 임계값 하향·한도 증가·LIVE 전환이 후보에 들어가면 **권고로만** 적는다.
   적용 여부는 운영자가 결정한다.
5. 운영자가 고르면 그때 예시 A 또는 B의 흐름으로 넘어간다.
