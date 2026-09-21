# Targeting Agent 작업 상태

Current Phase: 8 완료

Completed:
- Phase 1: 구조 / config.yaml / .env.example / SQLite / Logging
- Phase 2: Discovery Interface, CSV·JSON·URL Import, 후보 저장, 중복 제거
- Phase 3: Content Analyzer(heuristic + Gemini 옵션), Similarity, Target Score
- Phase 4: Comment Generator, Quality Filter, Duplicate Filter
- Phase 5: Action Queue, Rate Limiter
- Phase 6: ManualExecutor, Dry Run, Interaction 기록, Run Summary
- Phase 7: 로컬 Dashboard (stdlib http.server)
- Phase 8: 공식 Graph API 지원 범위 확인 →
  Hashtag Search 읽기 구현(7일 30개 한도 준수, urllib만 사용),
  OfficialAPIExecutor는 쓰기 미지원 확정(토큰 검증/health_check만 수행)
- Phase 10 일부: Feedback 기록 + 가중치 조정 제안(ML 없음)

공식 API 확인 결과(2026-09):
- 읽기 가능: ig_hashtag_search, {hashtag-id}/top_media|recent_media
  (instagram_basic + Public Content Access, 7일 고유 해시태그 30개,
   recent_media는 24시간, username 필드 요청 불가)
- 쓰기 불가: 타인 게시물 좋아요/댓글 엔드포인트 없음

알려진 제약:
- 해시태그 후보는 username을 받을 수 없어 unresolved:<media_id>로 저장되고
  safety.skip_unresolved_creator=true면 Action을 만들지 않는다.

Last Test:
python -m pytest targeting_agent/tests -q  → 72 passed

Pending:
- Phase 9: BrowserExecutor 필요성 검토
- Phase 10: Feedback → Target Profile 자동 반영
