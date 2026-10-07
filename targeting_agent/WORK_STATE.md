# Targeting Agent 작업 상태

Current Phase: 18A.5 완료 (수집 파이프라인 Hotfix — 실기 재실행 대기)

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

Phase 18A.1 완료: Instagram DOM / Selector Compatibility Hotfix
- 1차 Windows Smoke 결과: LOGGED_IN인데 검색어 5개 전부 SELECTOR_MISMATCH(20s timeout),
  Found 0 — Playwright/세션/runner/safety는 정상, 검색 화면 selector 불일치가 원인
- 검색 경로를 URL 추측(/explore/search/keyword/?q=) → 공개 Web UI navigation으로 교체
  (홈 → 검색 진입 → 입력 → 결과 목록 → 해시태그(없으면 계정) 결과 페이지 → /reel/ 수집)
- selector 우선순위를 aria-label/placeholder/role → href 패턴 → 최소 CSS로 정리,
  한국어+영어 라벨 병기(전체 언어 목록은 넣지 않음), nth-child·해시 class 미사용
- SelectorStage 도입: SEARCH_ENTRY / SEARCH_INPUT / SEARCH_RESULT / RESULT_PAGE /
  REEL_LINK / POST_DETAIL / USERNAME / CAPTION — 로그에 query·stage·selector key·예외 타입
- 디버그 스크린샷 파일명을 selector_<stage>_<timestamp>.png로 통일(기존 기능 재사용, HTML dump 없음)
- Run 상태: SUCCESS / PARTIAL / FAILED / SESSION_STOPPED (전부 실패인데 OK로 남던 문제 수정)
- 요약 정합성: 오류 = selector + 기타 총합, 세부 내역 함께 표시
- 대기 전략 분리: timeout_ms(이동) vs selector_timeout_ms(기본 4초, 단계당 1회 예산)
  → 실패 시 검색어당 ~4초(기존 20초×후보수)
- 테스트 18개 추가(FakeDOM으로 실제 selector registry 검증) — 총 52개, 전체 348 passed
- 미검증: 실제 Instagram DOM(개발 환경에 로그인 세션 없음) → 운영자 2차 Smoke로 확인

Phase 18A.2~18A.4 완료: Discovery 진단 · 추출 품질 · 파이프라인 연결
- browser_diagnostics.py: 제한된 DOM 진단(URL/title/textbox 접근성 속성/nav href/
  reel 수/main·dialog) JSON 1줄. HTML·쿠키·토큰·localStorage 수집 안 함.
- selector 실패 시 stage별 스크린샷 + 진단 자동 기록
- 추출 품질 계측(username/caption/detail_failed) + schema v9 컬럼
- tests/fixtures/instagram_dom + 실제 Playwright·Chromium selector 검증(로컬 127.0.0.1)
- Discovery → Ingestion → CandidateProcessor → Score → 댓글 → Dashboard E2E 테스트

Phase 18B 완료: Browser Action Executor (LIKE / COMMENT)
- action_selectors.py(쓰기 selector 분리) / browser_page.py(PlaywrightActionPage) /
  browser.py(BrowserExecutor: DISABLED·DRY_RUN·LIVE)
- 기존 Action Queue / RateLimiter / Approval Gate / Interaction 재사용(두 번째 Queue 없음)
- FOLLOW/DM/SAVE/SHARE 기능 자체 없음, 재시도 없음, 댓글은 기존 후보만 사용
- 중단: 로그인 필요 / Challenge / 경고 / 작업 차단 → Run 전체 halt
- core/database.approve_action() 추가(승인 지점 단일화)

Phase 18B.1 완료: Action Policy / REVIEW·AUTOPILOT
- actions/policy.py: 점수 → 추천 Action(기존 config 임계값 재사용), RunMode, auto_approve
- Queue 생성·Dashboard 기본 체크·자동 승인이 같은 규칙을 쓴다
- AUTOPILOT에서도 creator 미확인·댓글 없음·승인 상한을 지킨다

Phase 18C 완료: Autopilot Orchestration
- autopilot/runner.py: lock → Discovery → inbox → 분석 → Queue → 승인 → 실행 → 리포트
- CLI --autopilot, run_targeting_autopilot.bat, schema v10 autopilot_runs
- scheduler.autopilot=true면 Scheduled Run이 Autopilot 실행(기본 false)
- 실제 쓰기(dry_run=0 Interaction) 건수를 따로 집계

Phase 18C.1 완료: 운영 품질 모니터링
- learning/operations.py: Discovery/추출/Executor/Autopilot 지표 + 경고(DB만 읽음)
- learning/weekly_report.py + CLI --weekly-report: 결정론적 주간 보고서(LLM 호출 0)
- Daily Summary에 운영 품질 섹션, Dashboard 상단에 운영 상태 한 줄(REVIEW/AUTOPILOT 표시)
- Implicit Feedback 분리: 운영자 승인=학습 신호 / **Autopilot 자동 승인=신호 아님**
  (schema v11 action_queue.approved_by — 자기 강화 방지) / Action 실패=실행 건강 지표
- 임계값·Profile 자동 변경 없음(추천까지만)

최종 안전 상태: autopilot.enabled=false · browser_executor.mode=DRY_RUN ·
actions.execution.dry_run=true · scheduler.autopilot=false · browser_discovery.enabled=false
개발 중 실제 Instagram 쓰기 0건.

Phase 18A.5 완료: Candidate Collection Pipeline Hotfix
- 실기 2차 Smoke: LOGGED_IN / 발견 10 / 수집 0 / 오류 0 → 원인 특정
- 원인: 태그·프로필 그리드의 /<username>/reel/<code>/ 형태를 Phase 12A PATH_RE가 거부,
  _collect_one이 사유 없이 drop(silent skip) → 상세 페이지는 한 번도 열리지 않았다
- url_input.PATH_RE 확장(/<username>/reel|p|tv/<code>/, /reels/videos/<code>/),
  canonical은 그대로 하나. 예약 경로(explore/stories/direct/accounts)는 계속 거부
- POST_LINK_SELECTORS에 /reels/ 추가
- 모든 drop에 skip 사유 counter + links=collected+skip+상세실패 자가 검증 + 첫 3건 구조화 진단
- counter 의미 분리(result page / 링크 / Reel 발견 / 수집), EMPTY 상태 추가
- URL 경로 username을 상세 실패 시 보조로 사용
- 테스트 25개 추가(+ 실제 Chromium 실기 형태 fixture). 전체 465 passed
- 오프라인 리허설: 링크 9 → Reel 7 → 수집 7 / username 86% / caption 86% / Claude 0 / 쓰기 0
- Autopilot DRY_RUN 리허설: Queue LIKE 5·COMMENT 5 → 자동 승인 10 → 실행 성공 6·보류 4
  (보류는 creator_daily_limit), 실제 쓰기 0, 브라우저 미실행

Phase 18A.6 완료: 2026 Instagram 화면 대응 (운영자 Windows PC 실기)
- 실기에서 검색어 1개가 통째로 실패(stage=SEARCH_RESULT_NOT_FOUND / 수집 0) → 직접 화면을 열어 원인 3가지 확인
  1) /explore/ 검색 입력창 위에 다른 DIV가 겹쳐 click()이 TimeoutError
     → 웹 UI가 검색 제출 시 스스로 이동하는 공개 주소(/explore/search/keyword/?q=)를 1순위로,
       막히면 기존 클릭·입력 경로로 fallback, 클릭이 가로막히면 focus + 키 입력
  2) 검색 결과 그리드 21/21이 /p/<code>/ 였고 Reel 여부는 릴스 배지로만 보임
     → /p/ 도 수집하고 Reel 판정은 상세의 link[rel=canonical]로. 배지는 순서만 앞당김(버리지 않음)
  3) 상세 화면에 article/header/h1 없음 → 공개 og 태그에서 작성자·본문·반응 수·게시일 추출
- 실기 결과: Reel 발견 49 / 수집 49 / username 100% / caption 100% / selector 오류 0 / 쓰기 0

Phase 18D 완료: Live Readiness & Calibration
- 판정 CONDITIONAL_GO (기술 준비도 READY · Target 보정 신뢰도 LOW)
- 18D.1 감사: action_queue.executor(예정) vs interactions.executor(실제)를 Planned/Actual로 분리.
  schema 추가 없음. 생성 주체는 run_id ↔ autopilot_runs 로 유도. 연결 끊긴 실행 기록도 감사 공백으로 검출
- 18D.2 보정: Browser 후보가 60~70점대인 원인은 부적합이 아니라 '모름'이었다.
  followers·posted_at 미수집 → creator_fit/activity/engagement(가중치 0.50)가 중립값에 묶여 상한 75.0점.
  COMMENT 임계값 82는 도달 불가였다. 임계값 대신 **빠진 데이터**를 고쳤다 —
  og:description에 이미 있던 게시 날짜를 파싱해 posted_at을 채우니 상한 75.0 → 82.5
- 18D.3 댓글: 거절 11.5% / generic 1.6% / Template 11.5% / caption 근거 100% → Prompt 수정 근거 없음(유지)
- 18D.4 추정: 현재 설정 하루 예상 LIKE 5 · COMMENT 5. 단 자격 후보가 전부 import CSV이고
  Browser Discovery 후보는 0건 — 지금 수집 경로는 Action으로 이어지지 않는다
- 18D.5 가드: 이중 스위치(mode=LIVE AND dry_run=false)만 실제 쓰기를 연다는 것을 회귀 테스트로 고정.
  Configured/Effective를 모든 실행 시작에 한 줄로 기록
- 18D.6 Canary: Discovery → 분석 → Queue → 승인 → Browser Executor DRY_RUN → Report 전 구간,
  후보→실행 사슬 끊김 0, 실제 쓰기 0 (운영 DB 사본 사용)
- 18D.7/8: `--live-readiness` 로 결정론적 보고서 생성(LLM 호출 없음)
- 테스트 618건 통과(Phase 18D에서 130건 추가)

Phase 18E 구현 완료 / 실제 LIKE 미수행: Limited Live Canary (LIKE 전용)
- 상한을 코드에 박았다: 총 6 / 하루 3 / 1회 2 / 48시간 / 최소 8시간 간격
- config.yaml은 LIVE로 바뀌지 않는다 — 프로세스 메모리 사본에서만 전환하고,
  안쪽은 기존 BrowserExecutor 그대로라 두 스위치 계약을 똑같이 통과한다
- 한도는 data/live_canary_state.json 에 남는다(gitignore). Scheduler가 잘못 떠도
  상태가 '다 썼다'면 no-op. 상태 파일이 깨지면 '처음부터'가 아니라 FAILED로 막는다
- COMMENT 이중 차단: Queue에서 LIKE만 승인 + Executor가 호출 자체를 위반으로 중단
  (CanaryViolation은 PlatformWarningError 상속 — Action 단위 격리가 아니라 Run 중단)
- click 성공 ≠ 좋아요 남음. 새로고침해 유지되는지 확인하고, 확인 못 하면
  UNKNOWN_WRITE_STATE로 남기고 다시 누르지 않는다(재클릭은 좋아요 취소다)
- 대상 조건에 canonical_url 필수를 추가 — 실제로 열어 본 적 있는 게시물만 누른다
- 실제 Chromium fixture로 LIVE 경로 검증(누르기/확인/새로고침/중복/COMMENT 차단)
- 테스트 47건 추가, 전체 665 PASS

**실제 LIKE는 수행하지 않았다.** 자격 후보가 0건이기 때문이다.
  실제 후보 최고점 71.50 < LIKE 임계값 75
  75를 넘는 6건은 전부 CSV 샘플(canonical_url 없음) → 대상 아님
  오늘 Claude 20/20 소진으로 추가 분석 불가
  임계값을 낮추지 않고 NO_ELIGIBLE_CANDIDATES로 정상 종료했다.
  (18E.1에서 Resume Scheduler를 설치했다 — 깨우기만 하고 쓰기는 게이트가 정한다.)

Phase 18E.1 완료: Canary 자동 이어가기
- '기다리는 것'을 실패와 구분했다 — WAITING_AI_BUDGET / WAITING_ELIGIBLE
- **48시간 창을 '처음 실제로 누르려는 때'부터 센다.** 이전에는 Canary를 만든
  순간부터 세서, 후보가 없어 기다리는 동안 창이 흘러가 한 번도 못 눌러 보고
  만료될 수 있었다. 기다린 시간은 쓰기 기간이 아니다
- 합성 주소 차단을 자격 판정 안으로(_eligible_actions 한 곳에서만 판단).
  SAMPLE001 처럼 열어 본 적 없는 주소는 점수와 무관하게 대상이 아니다
- Claude 한도는 기존 집계(DAILY_METRIC + today_str)를 그대로 읽는다.
  reset 시각을 추측하지 않고, 남은 예산만큼만 Discovery 분석에 쓴다
- 상태 파일이 없는데 실제 LIKE 기록이 있으면 새 Canary를 열지 않고 멈춘다
- 동시 실행은 기존 SchedulerLock 재사용으로 막는다
- Windows Task 설치: DailyReels_Targeting_CanaryResume (8시간 간격, IgnoreNew)
  Task는 깨우기만 한다 — 실제로 누를지는 Runner의 상태·한도·게이트가 정한다
- 테스트 29건 추가, 전체 694 통과

현재 Canary 상태: WAITING_AI_BUDGET
  Claude 20/20 소진 · 자격 후보 0 · 실제 후보 최고점 71.50 (임계값 75)
  LIVE 창 미개시 · 실제 Instagram 쓰기 0
  한도가 회복되면 Scheduler가 Discovery → 분석 → 자격 확인 → 리허설 →
  제한된 실제 LIKE 까지 자동으로 이어간다(사용자 개입 불필요).

Next: Scheduler 자동 진행 관찰 → 첫 실제 LIKE → 재판정
  1) Claude 일일 한도 회복 후 run_targeting_discovery.bat --discover 로
     최신 Reel 수집 + 분석(최근 게시물일수록 activity 점수가 높아 75를 넘길 수 있다)
  2) run_targeting_canary_rehearsal.bat 로 리허설(실제 쓰기 0)
  3) run_targeting_live_canary.bat 로 첫 실제 Canary(최대 2건)
  4) 성공 확인 후에만 Scheduler 설치
Dashboard에서 사람 Feedback 축적(Target 보정 신뢰도 LOW → 상향)
Meta API: Optional (진행을 막지 않음)

Pending:
- LIVE 전환은 운영자 판단(browser_executor.mode / actions.execution.dry_run 둘 다 풀어야 함)
- 작성자 followers 수집 — Browser Discovery 후보가 Action 자격을 얻으려면 필요(18D.2)
- 사람 Feedback 10건 이상 축적 후 임계값 재검토(그 전에는 근거 없음)
- 실제 Instagram 계정/토큰으로 Hashtag Discovery 검증(권한 심사 필요)
- Gemini 분석/댓글 생성 실사용 검증(API Key 필요)
- 운영 데이터 축적 후 학습 임계값(min_keyword_media 등) 재조정
