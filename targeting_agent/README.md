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
| `run_targeting.bat --limit 5` | 이번 실행 최대 Action 수 |
| `run_targeting.bat --stats` | 현재 DB 현황만 출력 |
| `run_targeting.bat --dashboard` | 로컬 Dashboard(http://127.0.0.1:8501) |
| `run_targeting.bat --no-dry-run` | 실제 처리 대상 목록 생성(확인 대기 상태) |
| `run_targeting.bat --list-pending` | 확인 대기 중인 Action 목록 |
| `run_targeting.bat --confirm all` | 직접 처리한 Action을 기록(`--confirm 12,13`도 가능) |
| `run_targeting.bat --skip 14` | 처리하지 않기로 한 Action 취소 |

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

## 10. 안전 원칙

- 기본값은 Dry Run이며, 실제 자동 좋아요/댓글을 수행하지 않는다.
- Rate Limit은 **플랫폼 제한 우회가 아니라 운영자가 정한 내부 보수적 한도**다.
- 동일 게시물 / 동일 Creator / 동일 댓글 중복은 DB 제약과 필터로 차단한다.
- 광고·협찬·민감 콘텐츠·비공개 계정은 대상에서 제외한다.
- 탐지 우회, CAPTCHA 우회, Challenge 우회, 자동화 위장 기능은 구현하지 않는다.
- **BrowserExecutor는 구현하지 않는다**(Phase 9 검토 결과, `docs/phase9_browser_executor.md`).
  금지선을 지키면서 만들 수 있는 것이 사실상 없고, 제재 위험은 운영자 계정이 진다.
- 플랫폼 경고/인증 요구가 감지되면 자동 실행을 즉시 중단한다.

## 11. 테스트

```bash
python -m pytest targeting_agent/tests -q
```

## 12. 현재 구현 범위

| 상태 | 항목 |
| --- | --- |
| 구현 완료 | Config, SQLite, Logging, CSV/JSON/URL Import Discovery, 규칙 기반 분석, 유사도, Target Score, 댓글 생성/품질/중복 필터, Action Queue, Rate Limiter, ManualExecutor, Dry Run, Dashboard, Feedback 기록 |
| 선택 사용 | Gemini 분석/댓글 생성(API Key 필요) |
| 구현 완료(조건부) | HashtagDiscovery — 공식 API 토큰/권한 필요, Creator 미확인 제약 있음 |
| 미지원 확인 | OfficialAPIExecutor 쓰기(공식 API에 해당 기능 없음) |
| 구현 안 함(결정) | BrowserExecutor — Phase 9 검토 결과 미구현, `docs/phase9_browser_executor.md` 참고 |

자세한 진행 상태는 `WORK_STATE.md` 참고.
