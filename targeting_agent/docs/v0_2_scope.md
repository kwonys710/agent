# Targeting Agent v0.2 범위 설계

작성일: 2026-09-21 / 기준: v0.1(Phase 1~10) 완료 상태

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

### Phase 11 — 내 반응자 기반 Discovery (최우선)

B1의 실질적 해법. 해시태그와 달리 **내 소유 미디어의 댓글 작성자는 username을 받을 수 있다**
(본인 콘텐츠 범위라 권한 문제도 작다). 이미 내 콘텐츠에 관심을 보인 사람이므로 Target 품질도 높다.

- 새 Discovery 소스 `MyAudienceDiscovery`
  - 내 미디어 목록 → 각 미디어의 댓글 작성자 수집 → Creator 후보화
  - 해당 Creator의 최근 공개 게시물을 후보 media로 연결(가능한 범위 확인 필요)
- `discovery.default_source: my_audience` 추가, 기존 Import/Hashtag와 공존
- **사전 확인 필요(공식 문서)**: 댓글 작성자 필드(`from{id,username}`) 제공 여부와 권한,
  타 계정 미디어 조회 가능 범위, oEmbed로 permalink→작성자 해석 가능 여부

완료 기준: 토큰만 넣으면 Creator가 식별된 후보가 DB에 쌓이고, 그대로 Action Queue까지 간다
(`unresolved:` 후보가 생기지 않는다).

### Phase 12 — 후보 입력 보강

Phase 11이 권한 문제로 막혀도 B1을 완화하는 안전망.

- `--add <permalink> [--username <name>]`: 링크 하나를 바로 후보로 추가하는 CLI
- permalink만 주어졌을 때 username 해석 경로(공식 oEmbed 사용 가능 여부 확인 후 결정,
  불가하면 운영자 입력 요구 — 스크래핑은 하지 않는다)
- Import 파일 감시: `data/inbox/*.csv`를 실행 시 자동으로 읽고 처리 후 `data/inbox/done/`으로 이동

완료 기준: 휴대폰에서 본 릴스를 링크 복사 → 한 줄 명령으로 후보 등록.

### Phase 13 — AI 분석·댓글 실사용 검증 (B3)

- `GEMINI_API_KEY` 실제 연결 상태에서 분석/댓글 경로 검증(현재는 코드 경로만 존재)
- 프롬프트 파일화(`prompts/analysis_v2.txt`, `prompts/comment_v2.txt`) + 버전 관리
  → 프롬프트 변경 시 `prompt_version`이 바뀌어 캐시가 무효화되는 흐름 유지
- **비용 가드**: `analysis.max_api_calls_per_run`, `analysis.max_api_calls_per_day`,
  캐시 적중률 로깅, 한도 도달 시 heuristic으로 자동 강등
- 댓글은 Gemini 우선 / 실패·한도 시 템플릿 fallback (이미 있는 구조 활용)

완료 기준: 같은 후보를 두 번 돌려도 API 호출이 한 번만 나가고, 한도 초과 시 중단이 아니라 강등된다.

### Phase 14 — Dashboard에서 처리 (B2)

- 읽기 전용 → 로컬 조작 가능(127.0.0.1 바인딩 유지, 외부 노출 없음)
- 확인 대기 목록에서 버튼으로 `confirm` / `skip`
- 후보/Action 행에서 Feedback(GOOD_TARGET / NOT_MY_STYLE 등) 클릭 입력
- 댓글 후보 3개 중 선택 변경
- CSRF/오조작 방지를 위해 POST + 토큰(로컬 세션 한정), 외부 바인딩 시 실행 거부

완료 기준: 목록 확인 → 처리 → 피드백까지 브라우저 한 화면에서 끝난다.

### Phase 15 — 스케줄 실행과 요약 리포트 (B5)

- `run_targeting.bat --schedule-install` : Windows 작업 스케줄러 등록(매일 1회)
- 실행 요약을 `data/exports/summary_<날짜>.html`로 저장(오늘 수집/점수 분포/Action/한도 사용률)
- 연속 실패 시 다음 실행 중단 플래그(app_state) + 요약에 표시

완료 기준: 사람이 명령을 치지 않아도 매일 후보가 쌓이고, 아침에 요약 하나만 보면 된다.

### Phase 16 — 결과 자동 추적으로 학습 루프 닫기 (B4)

- Interaction 이후 N일 안에
  - 해당 Creator가 내 계정을 팔로우했는지
  - 내 게시물에 댓글/답글을 남겼는지
  를 공식 API 범위에서 주기적으로 확인 → `FOLLOWED` / `RESPONDED` Feedback 자동 기록
- 추적 결과를 `--learn` 제안에 반영(Phase 10 구조 그대로 사용)
- **사전 확인 필요**: 팔로워 목록 조회 가능 범위. 불가하면 "내 미디어 댓글 작성자"만으로 대체.

완료 기준: 운영자가 Feedback을 직접 입력하지 않아도 학습 표본이 쌓인다.

## 4. v0.2에서 하지 않는 것 (Non-goals)

- **BrowserExecutor** — Phase 9 결정 유지(`docs/phase9_browser_executor.md`)
- 팔로우/언팔로우 자동화, DM 자동 발송
- 다계정 운영, 클라우드 배포, 팀 기능
- 영상 다운로드 기반 시각 분석(비용·저작권·정책 리스크 대비 이득이 불명확)
- 형태소 분석기 도입 — Phase 13(AI 분석)으로 해결되면 불필요. 필요해지면 그때 재검토.

## 5. 선행 확인 항목 (코드 작성 전)

아래는 **공식 문서로 직접 확인한 뒤** 설계를 확정해야 한다.
(v0.1 작업 환경에서는 `developers.facebook.com` 접근이 차단되어 검색 결과로만 확인했다.)

1. 내 미디어 댓글 작성자 필드(`from{id,username}`) 제공 여부와 필요한 권한 → Phase 11
2. 타 계정(공개 Business/Creator) 미디어·프로필 조회 가능 범위 → Phase 11
3. Instagram oEmbed로 permalink → 작성자 해석 가능 여부와 권한 → Phase 12
4. 팔로워/팔로우 관계 조회 가능 여부 → Phase 16
5. 앱 심사(Public Content Access) 필요 범위와 소요 기간 → Phase 11·16 일정 전제

확인 결과 불가로 나오면 해당 Phase는 **범위를 줄이거나 Non-goal로 내린다.**
v0.1에서처럼 "가능하다고 가정한 코드"는 쓰지 않는다.

## 6. 데이터·설정 변경 예상

| 항목 | 변경 |
| --- | --- |
| `creators` | `resolved_at`, `source`(hashtag/my_audience/import), `followed_me_at` 컬럼 추가 |
| 신규 테이블 | `tracking_jobs`(Phase 16: Interaction 후 추적 대상과 확인 예정일) |
| `config.yaml` | `discovery.my_audience.*`, `analysis.max_api_calls_per_*`, `dashboard.allow_actions`, `schedule.*`, `tracking.*` |
| 마이그레이션 | 스키마 버전(`app_state.schema_version`) 기반 단계적 ALTER — 기존 DB를 지우지 않는다 |

## 7. 진행 순서 제안

```
11 (Discovery) → 13 (AI 품질) → 14 (Dashboard) → 16 (자동 추적) → 15 (스케줄) → 12 (입력 보강, 필요 시)
```

- 11이 막히면 12를 먼저 올려 후보 공급을 유지한다.
- 13은 API Key만 있으면 다른 Phase와 병행 가능하다.
- 15는 마지막이 적절하다. 자동 실행은 나머지가 안정된 뒤에 켜는 게 안전하다.

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
