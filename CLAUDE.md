# DailyReels Targeting Agent — 개발 운영 규칙

이 파일은 새 세션에서 자동으로 로드된다. 작업을 시작하기 전에 아래를 따른다.

## 1. 작업 시작 — Memory Recall

새 작업을 시작하면 **관련 있는 과거 교훈만** 먼저 찾는다. 전체를 읽지 않는다.

```bash
grep -n "<작업 영역 키워드>" targeting_agent/docs/continuous_improvement.md
head -30 targeting_agent/WORK_STATE.md   # 현재 상태만
```

영역 키워드 예: `lock`, `selector`, `url`, `score`, `통계`, `approval`, `test`.
관련 없는 교훈을 끌어오지 않는다(lock 작업에 comment 품질 교훈을 주입하지 않는다).

## 2. 개선 루프

단순 수정이 아니면 이 순서를 지킨다. 각 단계는 **증거**로 넘어간다.

```
OBSERVE → EVIDENCE → DIAGNOSE → PLAN → IMPLEMENT → VERIFY → REVIEW → REGRESSION → LEARN
```

- **Evidence-First.** 추측으로 고치지 않는다. 증상이 지표면 먼저 지표를 조사한다.
  (예: 신규율 하락 → 검색어 수를 늘리기 전에 Query Bank·중복률·Novelty를 본다)
- 같은 원인으로 **3회** 수정해도 해결되지 않으면 고치기를 멈추고 다시 진단한다.

## 3. 역할 분리 (`.claude/agents/`)

| 역할 | 모델 | 용도 |
| --- | --- | --- |
| explorer | haiku | 증거 수집, 심볼 탐색 (코드 수정 안 함) |
| architect | opus | 구조 영향·재발 위험·최소 설계 |
| executor | sonnet | 최소 diff 구현 |
| qa | sonnet | 테스트 설계·회귀 |
| reviewer | opus | **작성자와 분리된** 검토 |
| verifier | sonnet | 완료 주장 검증 |
| analytics | sonnet | 운영 지표(읽기 전용) |

**구현한 Agent가 자기 작업을 최종 승인하지 않는다.** Executor → Reviewer → Verifier를
각각 별도로 호출한다. Coordinator(메인 세션)가 merge·commit·push를 담당한다.

병렬은 **파일 교집합이 없을 때만**. 다음은 순차로 다룬다:
`core/schema.py`, `core/database.py`, `config.yaml`, `actions/policy.py`,
`actions/executors/*`, `discovery/browser_*`.

## 4. 자동으로 고쳐도 되는 것

증거와 테스트가 있으면 중간 확인 없이 수정한다.

버그, 통계 오류, 로그 오류, silent failure, stale lock, 중복 처리 오류,
fixture·test 문제, Dashboard 표시 오류, 안전한 검색어 스케줄링 개선.

## 5. 운영자 결정 영역 — 자동으로 바꾸지 않는다

아래는 **권고(Recommendation)까지만** 만든다. 코드가 스스로 완화하지 않는다.

- LIKE / COMMENT 임계값 **하향**
- 일일 한도 **증가**, creator 한도 완화, cooldown 축소
- `browser_executor.mode` LIVE 전환, COMMENT 공개 LIVE 활성화
- `autopilot.enabled`, `scheduler.autopilot` 켜기
- Safety Gate 약화, 플랫폼 경고 무시

## 6. 플랫폼 안전 — 학습 금지 방향

탐지 회피, 사람 행동 위장, fingerprint·proxy·UA rotation, rate-limit 탐색,
경고·Challenge 우회, 비공식 API·GraphQL 역분석, 네트워크 응답 가로채기.

플랫폼 이상 신호(경고·Challenge·작업 차단)에는 **활동량 감소 또는 STOP만** 허용한다.

## 7. Runtime 격리

개발 계층은 Instagram Runtime을 바꾸지 않는다. 실제 쓰기 경로는 항상 하나다.

```
Runtime Orchestrator → Safety Gate → Browser Lock → Browser Executor
```

여러 Agent가 동시에 Instagram Web을 조작하지 않는다. 개발용 Agent는 브라우저를 열지 않는다.

## 8. Lock 작업 체크리스트

lock을 건드리면 최소 다음을 확인한다(실제 운영 장애에서 나온 항목이다).

owner 이름 · PID · PID 생존 여부 · lock 파일 경로 · 생성 시각 · stale timeout · 복구 안내

## 9. 완료 Gate

완료를 선언하기 전에 모두 만족해야 한다.

- 남은 작업 0
- 관련 테스트 PASS → 모듈 테스트 PASS → (Phase gate면) 전체 회귀 PASS
- Reviewer 미해결 지적 0
- Verifier 증거 확보
- 테스트 수를 과거 숫자에 맞추지 않는다 — **현재 baseline을 직접 확인**한다

## 10. 기록 위치

| 파일 | 내용 |
| --- | --- |
| `targeting_agent/WORK_STATE.md` | **현재 상태만**(버전·Phase·알려진 이슈·마지막 회귀·다음 작업) |
| `targeting_agent/docs/continuous_improvement.md` | 재사용할 **교훈만**(장기 보존) |
| `targeting_agent/docs/omc_continuous_improvement.md` | 이 체계의 사용법 |

둘에 같은 내용을 중복 기록하지 않는다. 일시적 오타·lint·한 번뿐인 실수는 교훈으로 남기지 않는다.

## 11. Git

`.env` / `.env.example` / browser profile / runtime DB / runtime report는 커밋하지 않는다
(기존 `.gitignore` 유지). force push와 history rewrite를 하지 않는다.
사용자의 미커밋 변경을 임의로 되돌리지 않는다.
