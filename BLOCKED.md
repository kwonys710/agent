# BLOCKED — 없음 (해소됨)

최종 갱신: 2026-10-07 / 기준 커밋: Phase 18D

## 현재 Blocker

**없습니다.**

이전에 기록돼 있던 `HARD_BLOCK_LOGIN`(개발 환경에서 실제 Instagram Smoke Test
불가)은 해소되었습니다. 운영자 Windows PC에서 직접 실행해 확인했습니다.

| 항목 | 결과 (2026-10-07, 운영자 Windows PC) |
| --- | --- |
| `run_instagram_session.bat --check` | `LOGGED_IN` |
| 실제 Instagram 검색 / 수집 | 성공 — Reel 발견 49 / 수집 49 |
| Username / Caption 추출 | 100% / 100% |
| Selector 치명 오류 | 0 |
| 실제 Instagram 쓰기 | 0 (전 구간 DRY_RUN) |

그 과정에서 Instagram 2026 화면 변경 3가지를 찾아 고쳤습니다(Phase 18A.6) —
검색 입력창을 덮은 레이어, `/reel/` href가 사라진 검색 그리드, `article`/`header`/`h1`
이 없어진 상세 화면. 자세한 내용은 커밋 `9cdba1f` 메시지에 있습니다.

## 남은 것 — Blocker가 아니라 운영자 결정

LIVE 전환은 기술적으로 막혀 있지 않습니다. **운영자가 직접 결정할 일**입니다.

- Phase 18D 판정: `CONDITIONAL_GO` (기술 준비도 READY · 보정 신뢰도 LOW)
- 보고서: `targeting_agent/data/reports/live_readiness_latest.html`
  (`python -m targeting_agent.main --live-readiness` 로 언제든 다시 생성)

전환 전에 알아 둘 것 두 가지:

1. **Browser Discovery로 모은 후보는 아직 Action으로 이어지지 않습니다.**
   작성자 followers를 수집하지 않아 `creator_fit`·`engagement` 축이 중립값에
   묶이고, 그 때문에 점수 상한이 82.5점입니다. COMMENT 임계값(82)에 겨우 닿고
   대부분은 못 넘습니다. 임계값을 내리는 것보다 빠진 정보를 채우는 쪽이 맞습니다.
2. **사람 Feedback이 2건뿐이라 Target 보정 신뢰도가 LOW입니다.**
   Dashboard에서 승인/거절을 쌓으면 신뢰도가 올라가고, 그때 임계값 조정을
   근거 있게 검토할 수 있습니다.

## 아직 확인하지 못한 것(별개 사안)

- 실제 Instagram 계정/토큰으로 Hashtag Discovery(Meta API 권한 심사 필요)
- Gemini 분석/댓글 생성 실사용(API Key 필요)

둘 다 현재 경로(Browser Discovery + Claude Code CLI)를 막지 않습니다.
