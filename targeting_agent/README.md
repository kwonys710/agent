# DailyReels Targeting Agent v0.1

DailyReels 콘텐츠와 성격이 비슷한 Instagram Reel/Creator를 찾아 점수를 매기고,
게시물 맥락에 맞는 댓글 후보를 만들어 **Action Queue**를 구성한 뒤,
설정한 Executor로 처리하고 모든 기록을 SQLite에 남기는 도구다.

기존 DailyReels 영상 편집 파이프라인과 **완전히 분리된 모듈**이며, 기존 파일을 수정하지 않는다.

> v0.1 기본 동작은 **ManualExecutor + Dry Run**이다.
> 즉, 프로그램이 스스로 Instagram에 접속해 좋아요/댓글을 달지 않는다.
> 대신 "무엇을 할지" 목록(CSV)을 만들어 주고, 실제 처리는 운영자가 직접 한다.

## 1. 설치

Windows 기준(Python 3.11 이상 필요, 3.13 권장):

```bat
cd "G:\내 드라이브\DailyReels"
python -m venv .venv
.venv\Scripts\activate
pip install -r targeting_agent\requirements.txt
```

가상환경을 만들지 않아도 `run_targeting.bat`이 필요한 패키지를 자동으로 설치한다.

## 2. 실행

```bat
run_targeting.bat
```

옵션:

| 명령 | 설명 |
| --- | --- |
| `run_targeting.bat` | config.yaml 기준 실행(기본 Dry Run) |
| `run_targeting.bat --import my_list.csv` | 후보 파일 지정 |
| `run_targeting.bat --add "<Instagram URL>"` | 링크 한 건을 후보로 등록(여러 번 지정 가능) |
| `run_targeting.bat --import-only` | 후보 입력(inbox 포함)만 하고 분석·실행은 건너뜀 |
| `run_targeting.bat --limit 5` | 이번 실행 최대 Action 수 |
| `run_targeting.bat --stats` | 현재 DB 현황만 출력 |
| `run_targeting.bat --dashboard` | 로컬 Dashboard(http://127.0.0.1:8501) |
| `run_targeting.bat --no-dry-run` | 실제 처리 대상 목록 생성(확인 대기 상태) |
| `run_targeting.bat --list-pending` | 확인 대기 중인 Action 목록 |
| `run_targeting.bat --confirm all` | 직접 처리한 Action을 기록(`--confirm 12,13`도 가능) |
| `run_targeting.bat --skip 14` | 처리하지 않기로 한 Action 취소 |
| `run_targeting.bat --feedback GOOD_TARGET --target SAMPLE001` | Feedback 기록 |
| `run_targeting.bat --learn` | Feedback 기반 조정 제안 확인 |
| `run_targeting.bat --learn-apply` | 제안을 실제로 반영 |
| `run_targeting.bat --learn-reset` | 학습 반영 내용 초기화 |
| `run_targeting.bat --rescore` | 점수 미달로 제외된 후보를 재채점 대상으로 복귀 |

Python으로 직접 실행하려면 프로젝트 루트에서:

```bash
python -m targeting_agent.main
```

## 3. 후보(Candidate) 입력

기본 Discovery는 **CSV/JSON/URL 목록 Import**다.
해시태그 자동 수집(`discovery.default_source: hashtag`)은 공식 Graph API로 구현되어 있으나
아래 8-2의 권한과 제약을 확인한 뒤 사용해야 한다.

`targeting_agent/samples/candidates_sample.csv` 형식:

```csv
media_id,permalink,username,caption,hashtags,media_type,like_count,comment_count,posted_at,followers,is_private,is_ad,language
SAMPLE001,https://www.instagram.com/reel/SAMPLE001/,office_daily_kim,오늘도 야근 끝 #직장인,직장인 퇴근,REEL,420,23,2026-09-18,5200,0,0,ko
```

- `media_id`가 없으면 `permalink`의 shortcode를 사용한다.
- `.txt` 파일은 permalink만 한 줄씩 넣어도 된다.
- Excel에서 저장한 CSV(UTF-8 BOM)도 그대로 읽는다.

### 3-1. 링크로 바로 등록 (Phase 12A)

```bat
run_targeting.bat --add "https://www.instagram.com/reel/ABC123/"
run_targeting.bat --add "URL1" --add "URL2"
run_targeting.bat --add "URL" --username creator_a --caption "퇴근길 #직장인"
```

- `?utm_source=...` 같은 쿼리는 제거하고 `https://www.instagram.com/reel/<code>/` 형태로 저장한다
  → 같은 게시물이 중복 등록되지 않는다.
- **URL에 없는 정보(username, caption, 팔로워 수)는 추측하지 않는다.** 모르면 비워 두고
  상태를 `NEEDS_ENRICHMENT`로 남긴다. 이후 보강되면 분석 대상이 된다.
- `--username`, `--caption`을 주면 바로 분석 가능한 `NEW` 상태로 저장된다.

### 3-2. Inbox CSV 자동 처리 (Phase 12A)

`targeting_agent/data/inbox/*.csv`를 실행할 때 자동으로 읽는다.

```csv
url,username,note
https://www.instagram.com/reel/AAA/,,링크만 있어도 된다
https://www.instagram.com/reel/BBB/,creator_a,퇴근 브이로그
```

- 필수 컬럼 `url` (`permalink`/`link`도 인식), 선택 컬럼 `username` `note` `caption` `source`
- UTF-8 / UTF-8 BOM(Excel) / CP949 인코딩을 모두 읽는다
- **원본 파일을 삭제하지 않는다.** 처리 후 `data/inbox/processed/`로 옮기고,
  파일 자체를 읽을 수 없으면 `data/inbox/failed/`로 옮긴다
- 한 행이 잘못돼도 나머지는 처리하고, 실패 내역은 `<파일명>_report.md`로 남긴다
- 결과는 `ADDED / DUPLICATE / INVALID / ERROR`로 집계되어 `import_events` 테이블에 기록된다

> Phase 12A는 **후보 입력만** 담당한다. Instagram 페이지 접근·로그인·스크래핑은 하지 않는다.

## 4. 파이프라인

```
Discovery → 중복 제거 → 후보 저장 → 콘텐츠 분석 → 유사도 → Target Score
→ 필터 → 댓글 생성(3개) → 품질/중복 필터 → Action Queue → Rate Limit
→ Executor → Interaction 기록 → 일일 통계 → Run Summary
```

중간에 종료돼도 다시 실행하면 후보 상태(`NEW/ANALYZED/SCORED/QUEUED`)에 따라 이어서 처리한다.

## 5. 설정 (`targeting_agent/config.yaml`)

모든 운영 값은 config.yaml에서 관리한다. 주요 항목:

| 키 | 의미 | 기본값 |
| --- | --- | --- |
| `executor.mode` | manual / official_api / browser | `manual` |
| `actions.execution.dry_run` | 실제 실행 여부 | `true` |
| `scoring.minimum_target_score` | 최소 대상 점수 | 70 |
| `actions.require_score_for_like` | LIKE 기준 | 75 |
| `actions.require_score_for_comment` | COMMENT 기준 | 82 |
| `actions.daily_limits.likes / comments` | 하루 한도 | 20 / 8 |
| `actions.per_creator.max_actions_per_day` | Creator 하루 한도 | 1 |
| `actions.per_creator.count_mode` | `media`면 같은 게시물 LIKE+COMMENT를 1건으로 집계 | `media` |
| `comments.duplicate_similarity_threshold` | 댓글 중복 판정 | 0.86 |

Target Score 가중치(`scoring.weights`) 합계는 1.0이어야 하며, 아니면 실행 시 오류로 알려준다.

내 콘텐츠 성격(평일/주말 키워드 등)은 `targeting_agent/profiles/dailyreels.yaml`에서 수정한다.
코드 수정 없이 바꿀 수 있고, DB `app_state.target_profile`(JSON)에 값을 넣으면 그쪽이 우선한다.

## 6. Credential

`targeting_agent/.env` (`.env.example` 복사해서 사용):

```
GEMINI_API_KEY=
IG_ACCESS_TOKEN=
IG_BUSINESS_ACCOUNT_ID=
```

- Key가 없으면 AI 분석은 **규칙 기반(heuristic) 분석기**로 자동 대체되어 그대로 동작한다.
- 계정 ID/Password는 어디에도 저장하지 않는다.
- `.env`와 `data/*.db`는 git에 올라가지 않는다(.gitignore).

## 7. 실행 결과물

| 경로 | 내용 |
| --- | --- |
| `targeting_agent/data/targeting.db` | 후보/분석/댓글/Action/Interaction 기록 |
| `targeting_agent/data/exports/manual_actions_<run_id>.csv` | 수동 처리용 Action 목록 |
| `targeting_agent/data/logs/targeting_<날짜>.log` | 실행 로그(기본 30일 보관) |

## 8. Instagram 공식 API 지원 범위 (확인 결과)

### 8-1. 읽기 — 지원
| 항목 | 내용 |
| --- | --- |
| 엔드포인트 | `GET /ig_hashtag_search` → `GET /{hashtag-id}/top_media \| recent_media` |
| 필요 조건 | Instagram Business/Creator 계정, `instagram_basic` + Instagram Public Content Access(앱 심사) |
| 조회 한도 | **7일 동안 고유 해시태그 30개**, 페이지당 최대 50건 |
| recent_media | **최근 24시간** 공개 게시물만 반환 |
| 제약 | 반환 media 객체에 **`username` 필드를 요청할 수 없음** |

`.env`에 `IG_ACCESS_TOKEN`, `IG_BUSINESS_ACCOUNT_ID`를 넣고
`config.yaml`의 `discovery.default_source: hashtag`로 바꾸면 동작한다.
7일 한도는 `app_state.hashtag_quota`에 기록해 로컬에서 **초과하지 않도록 중단**한다
(우회 목적이 아니라 준수 목적).

**username을 받을 수 없으므로** 해시태그로 들어온 후보는 `unresolved:<media_id>`로 저장되고,
Creator 단위 한도/cooldown을 적용할 수 없다. 따라서 `safety.skip_unresolved_creator: true`(기본)
설정에서는 분석·점수까지만 하고 **Action은 만들지 않는다.**
운영자가 permalink를 열어 username을 확인한 뒤 CSV Import로 넣으면 정상 처리된다.

### 8-2. 쓰기 — 미지원
공식 API에는 **타인 게시물에 좋아요/댓글을 작성하는 엔드포인트가 없다.**
공개 댓글 작성 엔드포인트는 제거되었고, 이후 추가된 engagement 기능도 본인 소유 콘텐츠 기준이다.
그래서 `OfficialAPIExecutor`는 토큰 유효성만 확인하고 **어떤 쓰기도 수행하지 않는다**
(`executor.mode: official_api`로 실행하면 세션 검증 단계에서 중단된다).
→ 실제 좋아요/댓글은 `manual` 모드에서 운영자가 직접 처리한다.

> 위 내용은 구현 시점(2026-09)에 확인한 범위다. Meta 정책은 자주 바뀌므로
> 실제 토큰/권한을 받은 뒤 공식 문서로 한 번 더 확인할 것.

## 9. 실제 처리 흐름 (manual 모드)

`--no-dry-run`으로 실행해도 프로그램이 Instagram에 접속하지 않는다.
대신 처리 목록을 만들고 Action을 **확인 대기(APPROVED)** 상태로 둔다.

```bat
run_targeting.bat --no-dry-run     :: 1) 대상 선정 → 목록 생성(확인 대기)
                                   :: 2) data/exports/manual_actions_<run>.md 를 보고 직접 처리
run_targeting.bat --list-pending   :: 3) 대기 목록 확인
run_targeting.bat --confirm all    :: 4) 처리한 건을 기록(일부만 → --confirm 12,13)
run_targeting.bat --skip 14        :: 안 한 건은 취소
```

확인(`--confirm`) 시점에만 Interaction으로 기록되므로 **하지 않은 일이 성공으로 남지 않는다.**
확인 대기 건도 일일 한도 계산에 포함되어, 한도를 넘는 목록이 만들어지지 않는다.
Dry Run(기본)에서는 지금까지처럼 시뮬레이션으로 SUCCESS 처리된다.

## 10. Feedback 학습 (Phase 10)

Machine Learning 모델은 쓰지 않는다. Feedback을 모아 **Profile 키워드와 Score 가중치 조정안**을
계산하고, 운영자가 승인할 때만 반영한다.

### Feedback 기록

```bat
run_targeting.bat --feedback GOOD_TARGET  --target SAMPLE001      :: media_id
run_targeting.bat --feedback NOT_MY_STYLE --target 12 --note "이유"  :: Action ID
run_targeting.bat --feedback RESPONDED    --target @username       :: Creator
```

타입: `GOOD_TARGET`, `BAD_TARGET`, `NOT_MY_STYLE`, `GOOD_COMMENT`, `BAD_COMMENT`,
`LIKED`, `COMMENTED`, `RESPONDED`, `FOLLOWED`
(`LIKED`/`COMMENTED`는 실행 시 자동으로 기록된다.)

### 반영

```bat
run_targeting.bat --learn         :: 제안만 확인
run_targeting.bat --learn-apply   :: 반영(app_state에 override 저장)
run_targeting.bat --rescore       :: 기존에 제외된 후보를 다시 평가 대상으로
run_targeting.bat --learn-reset   :: 되돌리기
```

동작 규칙:

- **config.yaml과 profile 파일은 자동으로 수정하지 않는다.** 학습 결과는 DB(`app_state`)
  override로만 저장되고 `--learn-reset`으로 언제든 되돌릴 수 있다.
- 학습 신호는 **해시태그**만 사용한다(캡션 토큰은 '보는', '오늘도' 같은 어미가 섞여 부적합).
- 키워드가 Profile에 추가되려면 `min_feedback_samples`, `min_keyword_occurrences`,
  **서로 다른 게시물 `min_keyword_media`개 이상**을 모두 만족해야 한다.
- **운영자가 선언한 Profile 키워드는 Feedback으로 뒤집지 않는다.** 부정 신호가 쌓이면
  회피 목록에 넣는 대신 "확인 필요"로 보고한다(변형 형태 `카페에서`도 동일하게 보호).
- 반영 시 Profile version이 올라가 분석/점수 캐시가 무효화된다 →
  이후 실행에서 새 기준으로 다시 계산된다. 이미 처리된 후보는 `--rescore`로 되돌려야 다시 평가된다.
- `learning.profile_update_interval_days`(기본 7) 이내 재반영은 건너뛴다.

## 11. 안전 원칙

- 기본값은 Dry Run이며, 실제 자동 좋아요/댓글을 수행하지 않는다.
- Rate Limit은 **플랫폼 제한 우회가 아니라 운영자가 정한 내부 보수적 한도**다.
- 동일 게시물 / 동일 Creator / 동일 댓글 중복은 DB 제약과 필터로 차단한다.
- 광고·협찬·민감 콘텐츠·비공개 계정은 대상에서 제외한다.
- 탐지 우회, CAPTCHA 우회, Challenge 우회, 자동화 위장 기능은 구현하지 않는다.
- **BrowserExecutor는 구현하지 않는다**(Phase 9 검토 결과, `docs/phase9_browser_executor.md`).
  금지선을 지키면서 만들 수 있는 것이 사실상 없고, 제재 위험은 운영자 계정이 진다.
- 플랫폼 경고/인증 요구가 감지되면 자동 실행을 즉시 중단한다.

## 12. 테스트

```bash
python -m pytest targeting_agent/tests -q
```

## 13. 현재 구현 범위

| 상태 | 항목 |
| --- | --- |
| 구현 완료 | Config, SQLite, Logging, CSV/JSON/URL Import Discovery, 규칙 기반 분석, 유사도, Target Score, 댓글 생성/품질/중복 필터, Action Queue, Rate Limiter, ManualExecutor, Dry Run + 수동 확인 흐름, Dashboard, Feedback 기록/학습 반영 |
| 선택 사용 | Gemini 분석/댓글 생성(API Key 필요) |
| 구현 완료(조건부) | HashtagDiscovery — 공식 API 토큰/권한 필요, Creator 미확인 제약 있음 |
| 미지원 확인 | OfficialAPIExecutor 쓰기(공식 API에 해당 기능 없음) |
| 구현 안 함(결정) | BrowserExecutor — Phase 9 검토 결과 미구현, `docs/phase9_browser_executor.md` 참고 |

자세한 진행 상태는 `WORK_STATE.md` 참고.
