# Phase 9 — BrowserExecutor 필요성 및 구현 여부 검토

작성일: 2026-09-21 / 대상 버전: Targeting Agent v0.1

## 1. 검토 배경

Phase 8에서 확인한 사실:

- 공식 Instagram Graph API에는 **타인 게시물에 좋아요/댓글을 작성하는 엔드포인트가 없다.**
- 따라서 "Target으로 선정한 Reel에 좋아요/댓글"을 자동으로 수행하려면
  공식 API가 아닌 경로(브라우저 자동화)뿐이다.

Phase 9는 그 경로를 만들 것인지 결정하는 단계다.

## 2. 결론

**BrowserExecutor는 구현하지 않는다. `manual` 모드를 v0.1의 유일한 실행 경로로 유지한다.**

대신 수동 처리 흐름을 실제로 쓸 만하게 보강했다(4장).

## 3. 결정 이유

### 3-1. 프로젝트가 정한 금지선을 지키면서 만들 수 있는 것이 사실상 없다

요구사항 4항은 다음을 명시적으로 금지한다.

- 탐지 우회, CAPTCHA 우회, Challenge 우회, Rate Limit 우회
- 자동화를 사람 행동처럼 위장하는 기능

그런데 Instagram에서 브라우저 자동화로 좋아요/댓글을 계속 수행하면
**금지 기능을 전혀 넣지 않아도** 자동화로 식별되어 Challenge(본인 인증),
기능 제한, 계정 제재로 이어질 수 있다. 이때 동작을 계속 이어가려면
결국 위 금지 항목 중 하나를 건드려야 한다. 즉 "금지선을 지키는 BrowserExecutor"는
첫 Challenge에서 멈추는 도구이고, 멈추지 않게 만들려면 금지선을 넘게 된다.

### 3-2. 플랫폼 정책과 정면으로 충돌한다

공식 API가 해당 기능을 제공하지 않는 것은 기술적 미비가 아니라 정책적 선택이다
(공개 댓글 작성 엔드포인트는 과거에 있었다가 제거됐다).
우회 경로로 같은 동작을 자동화하는 것은 정책 위반이며,
제재 대상은 이 프로젝트가 아니라 **운영자 본인 계정**이다.

### 3-3. 얻는 것보다 잃는 것이 크다

이 프로젝트의 진짜 가치는 "누구에게 무엇을 남길지 고르는 판단"(Discovery→Score→Comment)에 있고,
마지막 클릭은 하루 20~28건 수준이다(config 기본 한도: LIKE 20 / COMMENT 8).
그 정도 분량을 자동화하려고 계정 정지 위험을 감수할 이유가 없다.
클릭을 없애는 대신 **클릭을 빠르고 정확하게 만드는 쪽**이 비용 대비 효과가 크다.

## 4. 대신 한 일 — 수동 실행 흐름 보강

| 문제 | 조치 |
| --- | --- |
| `--no-dry-run`인데 아무것도 안 하고 "SUCCESS"로 기록돼 일일 한도가 잘못 소모됨 | 실제 모드에서는 목록 생성 후 `APPROVED`(운영자 확인 대기) 상태로 두고, 운영자가 확인해야 Interaction으로 기록 |
| 목록이 CSV라 처리하기 번거로움 | 같은 실행에서 체크리스트 Markdown(`manual_actions_<run>.md`)도 생성 — permalink와 붙여넣을 댓글, Action ID 포함 |
| 확인 대기 건이 일일 한도에 안 잡힘 | Rate Limiter가 `APPROVED`(확인 대기) 건도 오늘 사용량에 포함 |
| 확인/취소 수단이 없음 | CLI `--list-pending`, `--confirm`, `--skip` 추가 |

사용 흐름:

```bat
run_targeting.bat --no-dry-run          :: 후보 선정 → 처리 목록 생성(확인 대기)
run_targeting.bat --list-pending        :: 대기 중인 Action 확인
                                        :: (Instagram에서 직접 좋아요/댓글 처리)
run_targeting.bat --confirm 12,13       :: 처리한 Action 기록(또는 --confirm all)
run_targeting.bat --skip 14             :: 처리하지 않기로 한 Action 취소
```

## 5. 재검토 조건

아래 중 하나가 성립하면 이 결정을 다시 검토한다.

1. Meta가 타인 콘텐츠에 대한 engagement(좋아요/댓글) 엔드포인트를 **공식 API로** 제공하는 경우
   → BrowserExecutor가 아니라 `OfficialAPIExecutor`를 확장한다.
2. 운영자가 계정 제재 위험을 명시적으로 수용하고, 자동화 사실을 숨기지 않는 범위에서
   제한적으로 사용하겠다고 결정하는 경우
   → 그때도 금지선(탐지/CAPTCHA/Challenge/Rate Limit 우회, 위장)은 유지하며,
     Challenge·인증 요구 감지 시 즉시 중단하는 구조로만 구현한다.

현재 `actions/executors/browser.py`는 위 결정을 코드로 고정한 상태다
(`validate_session()`이 항상 False, 모든 Action은 SKIPPED).
