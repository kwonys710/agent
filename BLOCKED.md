# BLOCKED — 실제 Instagram 검증 (Phase 18A.2 / 18B LIVE)

최종 갱신: 2026-10-07 (Phase 18A.5 수정 반영) / 기준 커밋: Phase 18A.5

## Blocker

`HARD_BLOCK_LOGIN` — 개발 환경에서 실제 Instagram Smoke Test를 수행할 수 없습니다.

확인한 사실(개발 환경 = Linux 컨테이너):

| 항목 | 상태 |
| --- | --- |
| `run_instagram_session.bat --check` | 실행 불가 — Windows 전용 배치, 이 환경은 Linux |
| Instagram 로그인 세션(`data/browser_profile/`) | 없음(운영자 PC에만 존재) |
| `https://www.instagram.com` 접근 | 차단(HTTP 응답 없음) |
| Playwright / Chromium | 설치됨 — **로컬 DOM fixture로는 정상 동작 확인** |

로그인은 운영자가 직접 해야 하고(ID/PW를 코드가 다루지 않는다는 원칙), 이 환경에서는
Instagram에 접근 자체가 불가능합니다. 그래서 selector를 실제 Instagram DOM으로
확인하는 단계만 운영자 PC에 남습니다.

## 그래서 대신 한 것

- Instagram 접근성 구조를 모사한 로컬 DOM fixture(`targeting_agent/tests/fixtures/instagram_dom/`)를
  127.0.0.1 서버로 띄우고, **실제 Playwright + 실제 Chromium**으로 운영 selector를 검증했습니다.
  (검색 진입 → 입력 → 결과 → 태그 페이지 → `/reel/` 수집 → username/caption 추출,
  그리고 좋아요 토글 · 댓글 게시 · 작업 차단 감지)
- 실패 시 `stage` + selector key + 제한된 DOM 진단 + 스크린샷을 남기도록 만들어,
  실제 환경에서 한 번 돌리면 어디를 고쳐야 하는지 바로 나옵니다.

## 2차 Smoke로 밝혀진 것 (해결 완료)

운영자가 보내 준 2차 결과(`발견 10 / 수집 0 / 오류 0`)로 원인을 특정해 수정했습니다.
태그·프로필 그리드의 Reel 링크가 `/<username>/reel/<code>/` 형태라서 URL 정규화에서
거부되고, 그 drop에 사유가 없어 오류 0으로 보였던 것입니다
(`docs/phase18a_browser_discovery.md` Phase 18A.5 참고).

이제 같은 명령을 다시 실행하면 수집이 되고, 혹시 빠지는 건이 있어도
`Skip 사유`와 `진단(최대 3건)`이 요약에 함께 출력됩니다.

## 사용자가 할 최소 행동 1개

운영자 PC(Windows)에서 아래 한 줄을 실행하고, 출력의 `상태 / 세션 / stage / keys`를 알려 주세요.

```bat
run_targeting_discovery.bat --discover-only --query "직장인"
```

- 로그인이 안 되어 있으면 먼저 `run_instagram_session.bat` 으로 직접 로그인하면 됩니다.
- 실패하면 `data/browser_debug/selector_<stage>_<시각>.png` 와 로그의 `DOM 진단` JSON 한 줄이
  남습니다. 그 두 가지만 있으면 selector를 정확히 한 곳만 고칠 수 있습니다.
- 전체 page HTML은 저장하지 않습니다. 쿠키·토큰·localStorage도 수집하지 않습니다.

## 현재 안전 상태 (그대로 두어도 아무 일도 일어나지 않음)

```
autopilot.enabled = false        # REVIEW 모드 — 자동 승인 없음
browser_executor.mode = DRY_RUN  # 실제 좋아요/댓글 없음
actions.execution.dry_run = true # 강한 가드
browser_discovery.enabled = false
scheduler.autopilot = false
```
