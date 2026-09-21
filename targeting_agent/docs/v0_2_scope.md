# Targeting Agent v0.2 범위 설계

작성일: 2026-09-21 / 기준: v0.1(Phase 1~10) 완료 상태
확정: 2026-09-21 (운영자 결정 반영 — Phase 분리, 진행 순서, Gemini 전략)

## 1. v0.1에서 실제로 확인된 병목

설계상의 추측이 아니라 v0.1을 만들고 돌리면서 드러난 문제만 적는다.

| # | 병목 | 근거 |
| --- | --- | --- |
| B1 | **후보가 자동으로 들어오지 않는다** | 공식 Hashtag Search는 `username`을 주지 않아 Creator를 식별할 수 없고, Creator 한도/cooldown을 적용할 수 없어 Action 대상에서 제외된다(`safety.skip_unresolved_creator`). 결국 CSV를 손으로 채워야 한다. |
| B2 | **수동 처리 왕복이 길다** | 목록 생성 → Instagram에서 직접 처리 → `--confirm` 3단계. Dashboard는 읽기 전용이라 확인/피드백을 CLI로만 할 수 있다. |
| B3 | **분석·댓글 품질이 규칙 기반 한계에 걸린다** | 형태소 분석 없이 토큰을 자르다 보니 '보는', '오늘도' 같은 어미가 키워드로 올라왔다(Phase 10에서 해시태그 전용으로 우회). 댓글도 템플릿 조합이라 중복 필터에 많이 걸린다. |
| B4 | **효과를 알 수 없다** | 기록되는 건 "내가 한 행동"뿐이다. RESPONDED/FOLLOWED는 운영자가 직접 입력해야 하므로 학습 루프가 사실상 닫히지 않는다. |
| B5 | **매번 수동 실행** | 스케줄 실행과 요약 리포트가 없다. |

## 2. v0.2 목표 한 줄

> **후보가 스스로 들어오고, 처리가 한 화면에서 끝나며, 결과가 자동으로 학습에 돌아오게 한다.**

## 3. 범위 (Phase 11~16)

우선순위 순. 각 Phase는 독립적으로 완료·릴리스 가능해야 한다.

### Phase 11A — Meta API Feasibility Spike (선행, 코드 최소)

대규모 구현 전에 **실제 계정 + 실제 응답**으로 가능 여부를 확정한다.
결과: `docs/phase11a_meta_api_spike.md`, Probe: `scripts/probe_meta_api.py`.

- 확인 대상: 내 Reel 댓글 조회 / comment `username` / `from{id,username}` /
  Consumer 계정 댓글 작성자 식별 / Profile 추가 조회 / 필요한 permission / follower relationship
- **"Public Content Access가 필요할 것"이라고 미리 가정하지 않는다.** 실제 사용할 endpoint와
  permission을 확인한 뒤 App Review 필요 여부를 판단한다.
- 판정: GO / LIMITED GO / NO-GO
- 현재 상태: **LIMITED GO(잠정)** — 문서 기준 검증 완료, 실계정 Probe 1회 실행 후 확정

### Phase 11B — Commenter Discovery (11A가 GO/LIMITED GO일 때만)

11A 결과에 따라 범위를 정한다. **Discovery와 Enrichment를 분리한다.**

```
내 Reel → Comments → Commenter 식별(username/IGSID) → Candidate 생성   (필수)
                                                        ↓
                                      Creator Profile Enrichment       (선택)
                                        ├ 가능 → followers/media_count 보강
                                        └ 불가 → 최소 정보만 저장, creator_fit 중립값
```

- username까지만 얻고 Profile 조회가 안 되는 경우도 **실패로 처리하지 않는다.**
- `discovery.default_source: my_audience` 추가, 기존 Import/Hashtag와 공존
- 완료 기준: `unresolved:` 후보가 생기지 않고 Action Queue까지 이어진다

### Phase 12A — Candidate Input Fallback (기본 입력 경로)

11A 결과와 무관하게 **먼저 만든다.** 11A가 NO-GO면 이것이 유일한 입력 경로가 된다.

- `--add <Instagram Reel URL> [--username <name>]` : 링크 단건 등록
- `data/inbox/*.csv` 자동 처리 → 완료분은 `data/inbox/done/`으로 이동
- permalink만 있을 때 username 해석 경로는 **공식 수단이 확인될 때만** 사용한다.
  확인 안 되면 운영자 입력을 요구한다 — 스크래핑·비공식 endpoint는 사용하지 않는다.

완료 기준: 휴대폰에서 본 릴스를 링크 복사 → 한 줄 명령으로 후보 등록.

### Phase 12B — Candidate Input UX 보강 (후순위)

일괄 등록, 중복 안내, 입력 이력 확인 등. 12A 운영 경험 후 착수.

### Phase 13 — AI Intelligence = Claude Code CLI (방향 변경, 완료)

**Gemini/OpenAI/Anthropic API SDK를 사용하지 않는다.** 로컬에 설치·로그인된
Claude Code CLI를 subprocess로 호출해 콘텐츠 이해를 맡긴다(운영자 결정, 2026-09-21).

- Python: ingestion, 정규화, SQLite, 캐시, 중복 제거, **결정론적 점수 계산**, Action Queue, 한도 관리
- Claude Code: 콘텐츠 이해, topic 분류, 의미적 관련성, 댓글 후보 3개

```yaml
ai:
  provider: claude_code
  model: sonnet
  daily_request_limit: 20
  max_candidates_per_run: 10
  max_turns: 1
  use_cache: true
  fallback_to_heuristic: true
  prompt_version: "1.0"
  prefilter_min_score: 60
```

- Cache Key = SHA256(model + prompt_version + normalized_candidate_input)
- 동일 Candidate + 동일 Prompt + 동일 Model → 재호출하지 않는다
- Pre-filter(heuristic) → 캐시 → 실행 한도 → 일일 한도 순으로 게이트
- 실패(CLAUDE_UNAVAILABLE / INVALID_JSON / INVALID_SCHEMA / TIMEOUT / USAGE_LIMIT / CLI_ERROR)는
  즉시 재시도하지 않고 heuristic fallback으로 진행한다
- Runtime Claude는 Repository를 보지 않는다(빈 임시 디렉터리에서 실행, 개발 세션 상속 금지)

### Phase 14 — Dashboard Action UI

읽기 전용 → 처리 가능. Candidate Card 단위로 보여준다.

- 카드 내용: username / target score / 선정 이유 / caption / 생성된 댓글 후보
- Action: `[승인]` `[Skip]` `[댓글 선택]` `[좋은 Target]` `[관심 없음]`
- Feedback: `[응답 있음]` `[팔로우됨]` `[좋은 댓글]` `[나쁜 댓글]`
- 클릭 결과는 즉시 SQLite에 기록(기존 confirm/feedback 경로 재사용)
- **127.0.0.1에서만 실행한다.** `0.0.0.0` 등 외부 인터페이스 바인딩이 감지되면 실행을 거부한다.
- 오조작 대비: POST + 로컬 세션 토큰, 되돌리기(`--skip`, `--learn-reset`) 유지

### Phase 15 — 스케줄 실행과 요약 리포트 (B5)

- `run_targeting.bat --schedule-install` : Windows 작업 스케줄러 등록(매일 1회)
- 실행 요약을 `data/exports/summary_<날짜>.html`로 저장(오늘 수집/점수 분포/Action/한도 사용률)
- 연속 실패 시 다음 실행 중단 플래그(app_state) + 요약에 표시

완료 기준: 사람이 명령을 치지 않아도 매일 후보가 쌓이고, 아침에 요약 하나만 보면 된다.

### Phase 16A — Response Tracking (우선)

공식 API로 확인 가능한 범위의 반응을 감지한다.

- 내 콘텐츠에 달린 새 댓글 / Reply / 기타 확인 가능한 Response
- 감지되면 `RESPONDED` Feedback 자동 기록 + `response_detected_at`, `response_type` 저장
- 추적 주기·대상 수 상한(`tracking.*`), 실패 시 지수 백오프

### Phase 16B — Follow-back Detection (조건부)

**Phase 11A 확인 결과: 공식 API에 팔로워 관계 확인 endpoint가 없다.**
따라서 자동 감지는 구현하지 않고 Dashboard 수동 Feedback으로 유지한다.
(`[팔로우됨]` `[응답 있음]` `[관심 없음]` 버튼 → feedback DB → 학습 반영)

비공식 follower API는 사용하지 않는다. 공식 지원이 생기면 그때 자동화를 재검토한다.

## 4. v0.2에서 하지 않는 것 (Non-goals)

- **BrowserExecutor** — Phase 9 결정 유지(`docs/phase9_browser_executor.md`)
- 팔로우/언팔로우 자동화, DM 자동 발송
- 다계정 운영, 클라우드 배포, 팀 기능
- 영상 다운로드 기반 시각 분석(비용·저작권·정책 리스크 대비 이득이 불명확)
- 형태소 분석기 등 대형 Dependency — Phase 13(AI 분석)으로 해결되면 불필요
- 비공식 Instagram API / 탐지·CAPTCHA·Rate Limit 우회
- 팔로우 자동화, DM 자동 발송

## 5. 선행 확인 결과 (Phase 11A)

상세: `docs/phase11a_meta_api_spike.md`

| 항목 | 문서 기준 결과 | 영향 |
| --- | --- | --- |
| 내 미디어 댓글 + `username`/`from{id,username}` | PASS (실측 대기) | Phase 11B 성립 |
| Consumer 댓글 작성자 식별 | LIMITED (실측 1순위) | 누락분은 후보에서 제외 |
| Profile Enrichment(`business_discovery`) | LIMITED — Facebook Login 전용 + 대상이 Professional일 때만 | Discovery와 분리 |
| App Review | **불필요(내 계정 범위)** — Standard Access로 충분 | 심사 전제 철회 |
| permission | IG Login: `instagram_business_basic`+`instagram_business_manage_comments` / FB Login: `instagram_basic`+`instagram_manage_comments`+`pages_read_engagement` | 기존 토큰 구조 유지 |
| follower relationship | 미지원 | Phase 16B 수동 유지 |

실측 Probe를 1회 실행해 판정을 확정하기 전까지 **Phase 11B 본 구현은 시작하지 않는다.**

## 6. 데이터·설정 변경 예상

| 항목 | 변경 |
| --- | --- |
| `creators` | `resolved_at`, `source`(hashtag/my_audience/import), `followed_me_at` 컬럼 추가 |
| 신규 테이블 | `tracking_jobs`(Phase 16: Interaction 후 추적 대상과 확인 예정일) |
| `config.yaml` | `discovery.my_audience.*`, `analysis.max_api_calls_per_*`, `dashboard.allow_actions`, `schedule.*`, `tracking.*` |
| 마이그레이션 | 스키마 버전(`app_state.schema_version`) 기반 단계적 ALTER — 기존 DB를 지우지 않는다 |

## 7. 진행 순서 (확정)

```
11A (Feasibility Spike)
  → 12A (Candidate Input Fallback 기본 확보)
  → 13  (Claude Code Runtime + Prompt Version + Cache + Heuristic fallback) ✔ 완료
  → 14  (Dashboard Action UI)
  → 11B (11A가 GO/LIMITED GO일 때만 Commenter Discovery 구현)
  → 16A (Response Tracking)
  → 16B (Follow-back: 공식 지원 여부에 따라 자동 또는 수동)
  → 15  (Schedule + Daily Summary HTML)
  → 12B (Candidate Input UX 보강)
```

- **11A가 NO-GO여도 v0.2 개발은 멈추지 않는다.** 12A가 입력 경로를 맡고 11B만 드롭된다.
- 13은 API Key 유무와 무관하게 착수 가능(Key 없으면 Adapter/Fallback까지 구현).

## 8. 릴리스 기준 (v0.2 Definition of Done)

1. 토큰/권한이 준비된 환경에서 **CSV를 손으로 만들지 않고** 후보가 쌓인다.
2. 운영자가 브라우저 한 화면에서 확인·피드백을 끝낸다.
3. 하루 실행이 자동으로 돌고 요약이 남는다.
4. 학습 표본이 수동 입력 없이 쌓인다.
5. 기본값은 여전히 **Dry Run + Manual**이며, 금지선(탐지/CAPTCHA/Challenge/Rate Limit 우회,
   자동화 위장, Credential 평문 저장)은 그대로 유지된다.
6. 전체 테스트 통과 + README/WORK_STATE 갱신.

## 9. 리스크

| 리스크 | 대응 |
| --- | --- |
| 공식 API 권한 심사 지연·거절 | Phase 12(수동 입력 보강)로 후보 공급 유지 |
| Meta 정책 변경으로 기존 기능 중단 | Discovery/Executor 계층 분리 유지, 실패 시 manual 경로로 강등 |
| AI 비용 증가 | 호출 한도 + 캐시 + heuristic 강등(Phase 13) |
| Dashboard 조작 기능의 오조작·노출 | 127.0.0.1 전용, 외부 바인딩 시 실행 거부, 되돌리기(`--skip`, `--learn-reset`) 유지 |
| 자동 추적이 과도한 API 호출 유발 | 추적 주기·대상 수 상한(`tracking.*`), 실패 시 지수 백오프 |
