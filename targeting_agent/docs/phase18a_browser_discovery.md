# Phase 18A — Instagram Browser Discovery

작성일: 2026-09-22 / 상태: 구현 완료(실 Instagram 스모크는 운영자 PC에서 수행)

## 1. 목적

후보를 사람이 일일이 링크로 넣지 않아도 되도록, Instagram 웹 화면에서 **검색 → Reel 후보 발견 →
공개 정보 읽기**까지를 자동화한다. 그 뒤는 기존 경로를 그대로 쓴다.

```
브라우저 검색 → 후보 URL/공개 텍스트 수집
  → url_input 정규화(Phase 12A) → CandidateIngestor 저장(Phase 12A)
  → CandidateProcessor 분석·채점·댓글 후보(Phase 12B)
  → Dashboard Review에서 운영자 승인(Phase 14 / 14.1)
```

## 2. 경계 — 하는 것 / 하지 않는 것

브라우저가 할 수 있는 동작은 다음 5가지뿐이다.

| 허용 | 페이지 열기 / 검색 / 스크롤 / 게시물 열기 / 공개 텍스트 읽기 |
| --- | --- |
| **금지** | 좋아요 · 댓글 · 팔로우 · DM · 저장 · 공유 등 **모든 쓰기 동작** |
| **금지** | 탐지 우회, CAPTCHA·Challenge·경고·Rate Limit 우회 |
| **금지** | stealth 플러그인, fingerprint 위장, UA/프록시/계정 rotation |
| **금지** | 비공식 API·GraphQL 역분석, 네트워크 응답 가로채기 |
| **금지** | ID/PW 자동 입력·저장, 쿠키·세션 토큰 추출 |
| **금지** | 비공개 계정 콘텐츠 접근 |

쓰기 동작은 "끄는 옵션"이 아니라 **코드 자체가 없다**(`tests/test_browser_discovery.py`의
보안 테스트가 해당 문자열이 소스에 없음을 검사한다).

로그인은 운영자가 `run_instagram_session.bat`에서 **직접** 한다. 세션은 Playwright
persistent profile(`data/browser_profile/`)에만 남고, git에 올라가지 않는다.

## 3. 세션 상태와 중단 규칙

| 상태 | 처리 |
| --- | --- |
| `LOGGED_IN` | 진행 |
| `LOGIN_REQUIRED` | **즉시 중단** — 운영자가 직접 로그인해야 한다 |
| `CHALLENGE` | **즉시 중단** — 본인 확인을 자동으로 풀지 않는다 |
| `PLATFORM_WARNING` | **즉시 중단** — 당분간 실행하지 않기를 권고 |
| `UNKNOWN` | 기본값 중단(화면 구조 변경 가능성) |

중단 시 `BrowserSessionError`가 올라오고, Run은 `SESSION_STOPPED`로 기록된다.
중단 사유는 `browser_discovery_runs.stop_reason`에 남는다.

## 4. 내부 상한 (플랫폼 제한 우회가 아니다)

운영자가 스스로 거는 보수적 한도다. 늘린다고 더 안전해지지 않는다.

| 항목 | 기본값 |
| --- | --- |
| `limits.max_queries_per_run` | 5 |
| `limits.max_candidates_per_query` | 10 |
| `limits.max_candidates_per_run` | 40 |
| `limits.max_analyze_per_run` | 20 |

## 5. Token Guard

Discovery는 Claude / Scorer / Comment Generator를 **직접 호출하지 않는다.**
분석은 `CandidateProcessor`에만 맡기고, 그 앞단에서 대상 수를 줄인다.

```
40 발견 → 중복 제거(canonical URL) → 신규(ADDED)만 분석 대상
→ 캡션/해시태그 없으면 NEEDS_ENRICHMENT(Claude 호출 없음)
→ AI 캐시 Hit은 호출하지 않음 → 일일/실행 한도 안에서만 Claude 호출
```

중복(DUPLICATE) 후보는 다시 분석하지 않는다.

## 6. 실행

```bat
run_instagram_session.bat              REM 운영자가 직접 로그인(최초 1회)
run_instagram_session.bat --check      REM 로그인 상태만 확인
run_instagram_session.bat --reset      REM 세션 삭제(DELETE 입력 확인 필요)

run_targeting_discovery.bat                        REM 수집 + 분석
run_targeting_discovery.bat --discover-only        REM 수집·저장까지만(Claude 호출 0)
python -m targeting_agent.main --discover --query 직장인브이로그
```

`browser_discovery.enabled`는 기본 `false`이며, `--discover`로 실행할 때만 켜진다.
**Scheduler와 자동으로 연결하지 않는다** — Scheduled Run은 브라우저를 열지 않는다.

## 7. 화면 구조 변경(selector) 대응

selector는 `discovery/browser_selectors.py` 한 곳에만 둔다. 화면이 바뀌면 이 파일만 고친다.

- 검색어 하나가 실패해도 나머지 검색어는 계속 처리한다(`SELECTOR_MISMATCH`로 기록).
- 상세 화면을 읽지 못한 후보는 저장하지 않는다(빈 값으로 추측해 넣지 않는다).
- 실패 시에만 스크린샷을 `data/browser_debug/`에 남긴다(기본 최대 10장, git 제외).
  전체 HTML은 저장하지 않는다.

## 8. 기록

`browser_discovery_runs`(schema v8) — 시작/종료, 상태, 세션 상태, 검색어 수,
발견/수집/신규/중복, 분석/정보부족, Claude 호출·캐시, 오류, 중단 사유.
후보 자체는 기존 `import_events` + `candidate_media`에 `source='instagram_browser_search'`로 남는다.

## 9. 남은 위험 (운영자 판단 사항)

- 로그인 상태에서의 자동 수집은 Instagram 이용약관의 자동화 수집 조항에 저촉될 수 있다.
  계정 제재 위험은 **운영자 계정이 진다**. 이 기능을 쓸지는 운영자가 결정한다.
- 화면 구조는 예고 없이 바뀐다. selector 실패가 반복되면 수집량이 0이 될 수 있다.
- 실 Instagram 스모크 테스트는 로그인 세션이 있는 운영자 PC에서만 가능하다.
