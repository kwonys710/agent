# Targeting Agent 작업 상태

Current Phase: 18A 완료 (Instagram Browser Discovery — 읽기 전용, 기본 OFF)

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
- Phase 9: BrowserExecutor 구현하지 않기로 결정(docs/phase9_browser_executor.md).
  대신 수동 실행 흐름 보강 — 실제 모드는 APPROVED(확인 대기)로 남기고
  --list-pending/--confirm/--skip 으로 처리, 확인 대기 건도 일일 한도에 포함,
  체크리스트 Markdown 생성
- Phase 10: Feedback 입력 CLI(--feedback/--target), 해시태그 기반 키워드 학습,
  Profile/가중치 override(app_state) 반영·초기화, --rescore 재채점,
  Profile 키워드 역전 방지 가드(변형 형태 포함)

공식 API 확인 결과(2026-09):
- 읽기 가능: ig_hashtag_search, {hashtag-id}/top_media|recent_media
  (instagram_basic + Public Content Access, 7일 고유 해시태그 30개,
   recent_media는 24시간, username 필드 요청 불가)
- 쓰기 불가: 타인 게시물 좋아요/댓글 엔드포인트 없음

알려진 제약:
- 해시태그 후보는 username을 받을 수 없어 unresolved:<media_id>로 저장되고
  safety.skip_unresolved_creator=true면 Action을 만들지 않는다.

Last Test:
python -m pytest targeting_agent/tests -q  → 101 passed

Next (v0.2 확정 — docs/v0_2_scope.md):
순서: 11A → 12A → 13 → 14 → 11B → 16A → 16B → 15 → 12B

Phase 11A 완료(문서 기준): docs/phase11a_meta_api_spike.md, scripts/probe_meta_api.py
- 판정: LIMITED GO(잠정) — 실계정 Probe 1회 실행 후 확정
- App Review는 전제에서 제외(내 계정 범위면 Standard Access로 충분)
- Profile Enrichment는 business_discovery(FB Login + Professional 대상)로 제한
  → Commenter Discovery와 분리
- follower relationship은 공식 미지원 → Phase 16B는 수동 Feedback 유지
- 실측 1순위: Consumer 계정 댓글 작성자의 from/username 반환 여부

Phase 12A 완료: Candidate Input Fallback
- CLI `--add <URL>`(다건 가능), `--import-only`
- `data/inbox/*.csv` 자동 처리 → processed/ 또는 failed/ 로 이동(삭제하지 않음)
- URL 검증·정규화(쿼리 제거, /reels/→/reel/), 중복 우선순위: instagram_media_id → canonical_url
- 부분정보 후보는 NEEDS_ENRICHMENT 상태로 저장(파이프라인을 막지 않음)
- import_events 테이블에 입력 이력 기록(ADDED/DUPLICATE/INVALID/ERROR)
- schema v2 마이그레이션: 기존 DB에 canonical_url/instagram_media_id 컬럼만 추가

Phase 13 완료 (방향 변경: Claude Code Runtime, LLM API SDK 미사용)
- 13A Probe: claude 2.1.278, `claude -p --output-format json` 실제 호출 성공 → GO
- ai/claude_runner.py: subprocess 호출(프롬프트는 stdin, 빈 임시 cwd, --restricted,
  Bash/Edit/Write 금지, --strict-mcp-config, 개발 세션 상속 금지, 재시도 없음)
- ai/schema.py: Pydantic 검증. CLI wrapper 성공과 payload 유효성을 분리해서 본다
- ai/cache.py: SHA256(model+prompt_version+정규화 입력), 동일 후보 재호출 차단
- ai/intelligence.py: 캐시 → Pre-filter → 실행 한도 → 일일 한도 게이트, heuristic fallback
- 댓글은 Claude 후보 우선, 부족하면 템플릿 보충. 품질·중복 필터는 동일 적용
- Target Score는 Python이 계산하고 Claude는 content_similarity에만 반영

Phase 14 완료: Dashboard Action UI (stdlib http.server 유지, 신규 의존성 없음)
- dashboard/{queries,service,app}.py 로 조회·상태변경·UI 분리
- Review / Action Queue / Feedback·Stats 3개 Tab
- 댓글 선택 및 직접 수정(원본 보존, generator=operator), LIKE/COMMENT 승인,
  Skip, 검토 대기로 되돌리기, Feedback 6종(중복 방지)
- 승인은 status=PENDING + approved_at 기록.
  Phase 9의 APPROVED(수동 처리 후 확인 대기)와 의미가 달라 컬럼으로 분리했다.
- 127.0.0.1 전용(외부 바인딩 시 실행 거부), POST 토큰 확인
- Dashboard는 Claude를 호출하지 않고 저장된 분석 결과만 표시한다

Phase 14.1 완료: Approval Gate Hardening
- 발견: 실행 대상 조회가 status='PENDING'만 보고 있어 승인하지 않은 자동 생성 Action도
  실행될 수 있었다.
- fetch_pending_actions → fetch_executable_actions 로 바꾸고
  조건을 status='PENDING' AND approved_at IS NOT NULL 로 강화
- count_unapproved_actions 추가 → Run Summary에 '승인 대기 N건' 표시
- Dashboard Action Queue 탭: 승인 대기 / 실행 대기(승인됨) 구분
- 기존 Safety(일일 한도/Creator cooldown/중복 Interaction)는 그대로 적용됨을 테스트로 고정

Phase 12B 완료: Dashboard Candidate Input + 즉시 처리
- Review 상단 '새 Candidate 추가'(URL 필수 / username·caption·note 선택)
- [추가만] = Claude 호출 0, [추가 후 분석] = 조건 충족 시에만 Intelligence 호출
- pipeline.CandidateProcessor 신설(분석·채점·댓글까지, Action Queue는 만들지 않음)
- URL 검증/정규화/중복은 Phase 12A Ingestion 재사용, source=dashboard_manual
- import_events.note 추가(schema v5) — 후보 테이블은 변경 없음
- 입력 길이 제한, POST 토큰, localhost 전용 유지

방향 변경: Meta API(Phase 11A/11B)는 필수 경로에서 제외 → Optional/Future.
후보 입력은 Dashboard URL / CLI --add / inbox CSV 세 경로로 충족.

Phase 15 완료: Scheduler + Daily Summary
- python -m targeting_agent.main --scheduled / run_targeting_scheduled.bat
- inbox 처리 → NEW 후보만 분석(CandidateProcessor 재사용) → 통계 → Summary HTML
- 파일 lock(중복 실행 방지, stale 회수, 비정상 종료 시에도 정리), scheduled_runs 이력
- data/reports/daily_summary_*.html + latest.html, keep_days 기준 해당 파일만 정리
- Scheduled Run은 Action Executor를 실행하지 않는다(승인된 Action 포함)
- Windows Task Scheduler helper: setup_scheduler.bat / scripts/install_scheduler.ps1
  (기본 Dry Run, -Install/-Uninstall, 공백·한글 경로 quoting 처리)

Phase 17 완료: Learning & Operational Stabilization
- learning/topics.py: Feedback·승인·Skip → topic 신호 → 결정론적 weight 조정
  (표본 기준, 1회 변화 상한 0.05, 0.5~1.5 범위, 근거 문자열 포함)
- learning/profiles.py: target_profiles 버전 관리(v1→v2…), rollback, 기존 버전 보존
- learning/comment_stats.py: 선택/수정 댓글 지표(길이·이모지·수정량·유사도)
- learning/metrics.py: 승인율·Score 구간별 승인율·Claude vs Heuristic·운영 상태·경고
  + 임계값 추천(설정 자동 변경 없음)
- scorer: topic weight를 content_similarity에만 적용, breakdown에 profile_version 기록
- CLI: --learn(미리보기) / --learn-apply(새 버전 생성) / --learning-rollback
- Dashboard Feedback/Stats 탭 + Daily Summary에 Learning 섹션
- 학습 경로 Claude 호출 0, 과거 후보 점수 자동 재계산 없음

Phase 17.1 완료: 운영 UX / 통계 Hotfix
- Action 체크박스 기본값을 Target Score와 config threshold로 결정
  (Score 66 → 둘 다 해제 + 기준 안내). 기존 Queue 선택과 SKIPPED 상태는 덮어쓰지 않는다.
  임계값은 추천이지 차단이 아니다(수동 override 가능).
- 통계 버그 수정: Claude 분석 후보는 heuristic 행과 claude_code 행을 함께 갖는데
  집계가 '행 수'를 세어 같은 후보가 양쪽에 중복 집계됐다(화면상 Claude 7 / Heuristic 7).
  후보별 최종 분석 방식 기준 distinct 집계로 교체.
- '분석 완료' 카드도 같은 기준(오늘 분석된 후보 수)으로 통일 — daily_stats 카운터는
  파이프라인 실행분만 세어 Dashboard 분석분이 빠져 있었다.
- 같은 수정을 Daily Summary와 Claude vs Heuristic 승인율에도 적용.
- Approval Gate / Learning / Executor / schema 변경 없음.

Phase 18A 완료: Instagram Browser Discovery (읽기 전용, 기본 OFF)
- discovery/browser_models.py: SessionState / PostDetail / QueryResult / DiscoveryStats
- discovery/browser_selectors.py: selector·세션 판정 문자열을 한 파일에 모음(화면 변경 대응 지점)
- discovery/browser_instagram.py: BrowserPage Protocol + PlaywrightBrowser(persistent profile)
  + InstagramBrowserDiscovery(검색→후보 수집, 검색어 단위 실패 격리)
- discovery/browser_runner.py: lock → 세션 확인 → Discovery → CandidateIngestor
  → CandidateProcessor → browser_discovery_runs 이력
- scripts/init_instagram_session.py + run_instagram_session.bat: 운영자가 직접 로그인
  (--check 상태 확인 / --reset 은 'DELETE' 입력 확인 필요)
- run_targeting_discovery.bat, CLI --discover / --discover-only / --query
- schema v8: browser_discovery_runs (추가 전용, 기존 테이블 변경 없음)
- 브라우저 동작은 열기/검색/스크롤/게시물 열기/공개 텍스트 읽기까지.
  좋아요·댓글·팔로우·DM·저장·공유, 탐지/CAPTCHA/Challenge/Rate Limit 우회,
  stealth·fingerprint·proxy·계정 rotation, 비공식 API·GraphQL, 응답 가로채기,
  ID/PW 자동입력, 쿠키·세션 토큰 추출 — 모두 코드 자체를 두지 않음(테스트로 검사)
- LOGIN_REQUIRED / CHALLENGE / PLATFORM_WARNING / UNKNOWN → 즉시 중단(SESSION_STOPPED)
- 내부 상한(운영자 한도, 우회 아님): 검색어 5 / 검색어당 10 / 실행당 40 / 분석 20
- Token Guard: 신규(ADDED) 후보만 분석, 중복 재분석 없음, 캡션 없으면 NEEDS_ENRICHMENT,
  Claude 호출은 CandidateProcessor의 캐시·한도 가드를 그대로 통과할 때만
- 수동 입력 경로(Dashboard URL / --add / inbox CSV)는 그대로 유지(기본 경로)
- Scheduler 자동 연결 없음(테스트로 검사), 기본값 browser_discovery.enabled=false
- 테스트 34개 전부 FakeBrowser 기반 — 실제 Instagram/Claude CLI 호출 없음
- 미수행: 실 Instagram 스모크(로그인 세션이 있는 운영자 PC에서만 가능)

Next: 운영 관찰 / v0.3 검토
Meta API: Optional (진행을 막지 않음)

Pending:
- Phase 18A 실 Instagram 스모크 테스트(운영자 PC: 로그인 → --discover-only → selector 확인)
- 실제 Instagram 계정/토큰으로 Hashtag Discovery 검증(권한 심사 필요)
- Gemini 분석/댓글 생성 실사용 검증(API Key 필요)
- 운영 데이터 축적 후 학습 임계값(min_keyword_media 등) 재조정
