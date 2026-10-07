# Phase 11A — Meta API Feasibility Spike

작성일: 2026-09-21 / 상태: **문서 기준 검증 완료, 실계정 Probe 대기**

## 0. 이 문서의 한계 (먼저 밝힘)

- 이 작업 환경에서는 `developers.facebook.com`이 **네트워크 정책상 차단**되어 1차 문서를 직접 열지 못했다.
  아래 "문서 기준" 결과는 공개 레퍼런스/검색 결과를 교차 확인한 것이며, **1차 출처 확인이 남아 있다.**
- 또한 이 환경에는 Instagram 계정과 Access Token이 없어 **실제 API 응답을 받지 못했다.**
- 그래서 판정은 **잠정(LIMITED GO)** 이고, 확정은 운영자 PC에서 Probe(4장)를 1회 실행한 뒤 내린다.
  Phase 11B 본 구현은 그 전까지 시작하지 않는다.

## 1. 검증 결과표

| 검증항목 | 결과 | 실제 응답/공식근거 | v0.2 영향 |
| --- | --- | --- | --- |
| 내 Reel 댓글 조회 | **PASS(문서)** / 실측 TBD | `GET /{ig-media-id}/comments` — 본인 소유 미디어 대상. Instagram Login·Facebook Login 모두 경로 존재 | Phase 11B의 전제 성립 |
| comment username | **PASS(문서)** / 실측 TBD | 반환 필드에 `username`, `from{id,username}` 포함 | Creator 식별 가능 → `unresolved:` 문제 해소 |
| commenter id | **PASS(문서)** / 실측 TBD | `from.id` = IGSID(앱 범위 식별자) | 중복 제거·추적 키로 사용 |
| Consumer commenter | **LIMITED** | "Personal 계정은 API 사용 주체가 될 수 없다"는 제약은 **API를 쓰는 내 계정**에 대한 것. 댓글 작성자가 Consumer일 때 `from`/`username`이 항상 채워지는지는 1차 문서로 확정하지 못함(레퍼런스에 "댓글 소유자가 조회할 때만 User 반환" 취지의 서술 존재) | **실측 필수 항목 1순위.** 누락분은 Candidate에서 제외 |
| Profile 조회(Enrichment) | **LIMITED** | `GET /{ig-user-id}?fields=business_discovery.username({target}){...}` — ① **Facebook Login 전용** ② 대상이 **Professional 계정일 때만** ③ 연령 제한 계정은 미반환 ④ 반환된 media id로 별도 GET 불가(중첩 조회만) | Consumer 댓글 작성자는 팔로워 수 등 조회 불가 → **Discovery와 Enrichment 분리 확정** |
| App Review 필요 | **NO (내 계정 한정 범위에서)** | "내 Instagram Professional 계정 또는 내가 관리하는 계정만 대상이면 **Standard Access**로 충분. 내가 소유·관리하지 않는 계정을 서비스하려면 Advanced Access(App Review)" | **Public Content Access 전제는 철회.** Phase 11B는 심사 없이 착수 가능성 높음. business_discovery까지 쓰려면 Facebook Login + Page 연결이 추가 조건 |
| 필요한 permission | 확정(문서) | **Instagram Login**: `instagram_business_basic`(프로필·미디어·댓글 읽기) + `instagram_business_manage_comments`(댓글 읽기/작성/숨김). **Facebook Login**: `instagram_basic` + `instagram_manage_comments` + `pages_read_engagement`(+ Page 연결) | v0.1의 `IG_ACCESS_TOKEN`/`IG_BUSINESS_ACCOUNT_ID` 구조 그대로 사용 가능 |
| follower relationship | **FAIL(미지원)** | 공식 API에 팔로워/팔로잉 목록 또는 관계 확인 endpoint 없음("Read anyone's follower/following lists — not exposed") | **Phase 16B는 자동화하지 않고 Dashboard 수동 Feedback으로 유지** |

읽기 전용 기준이며, 이 스파이크에서 쓰기 동작은 검토·구현하지 않았다.

## 2. 판정

### **LIMITED GO (잠정)**

근거:

1. **Commenter Discovery는 성립한다** — 내 미디어 댓글에서 `username`/`from.id`를 받을 수 있고,
   내 계정만 대상이면 App Review 없이 Standard Access로 가능하다.
2. **Profile Enrichment는 성립하지 않는 경우가 많다** — `business_discovery`는 Facebook Login 전용이고
   대상이 Professional 계정이어야 한다. 내 릴스에 댓글 다는 사람 상당수는 Consumer 계정일 것이므로
   팔로워 수 기반 `creator_fit`을 못 채우는 후보가 생긴다.
3. **Follow-back은 공식 경로가 없다** — 자동 감지 대상에서 제외한다.

따라서 사용자가 지시한 대로 **Commenter Discovery와 Creator Profile Enrichment를 분리**하는 설계가 맞다.

```
내 Reel → Comments → Commenter 식별(username/IGSID)
                      ↓
              Candidate 생성 (여기까지가 Phase 11B 필수)
                      ↓
          Profile Enrichment (선택 단계)
            ├ 가능(Facebook Login + Professional 대상) → followers/media_count 보강
            └ 불가 → 최소 정보로 저장, creator_fit은 중립값(0.5) 사용
```

`creator_fit`은 이미 팔로워 정보가 없을 때 중립값 0.5를 쓰도록 구현돼 있어(`analysis/profile_analyzer.py`)
Enrichment 실패가 파이프라인을 막지 않는다.

### 확정 전환 조건

| 실측 결과 | 판정 | 다음 행동 |
| --- | --- | --- |
| 댓글 조회 PASS + username PASS + Profile PASS | GO | Phase 11B 전체 구현(Enrichment 포함) |
| 댓글 조회 PASS + username PASS + Profile FAIL/LIMITED | LIMITED GO | Phase 11B는 Discovery만, Enrichment는 선택 단계 |
| 댓글 조회 FAIL 또는 username FAIL | NO-GO | Phase 11 중단, **Phase 12A를 앞으로 당김**(억지 구현·비공식 우회 없음) |

## 3. 남은 확인 항목 (실측으로만 확정 가능)

1. **Consumer 계정 댓글 작성자의 `from`/`username` 반환 여부** — 최우선
2. Instagram Login 토큰으로 `/{media-id}/comments`가 실제로 열리는지 (권한 조합 확인)
3. 댓글 작성자의 최근 게시물을 후보 media로 연결할 수 있는지
   (business_discovery 중첩 조회로만 가능 → Facebook Login + Professional 대상 한정)
4. 한 번 조회로 받을 수 있는 댓글 수와 페이지네이션 동작

## 4. Probe 실행 방법 (운영자 PC)

`targeting_agent/scripts/probe_meta_api.py` — 읽기 전용, 쓰기 동작 없음, 실패 시 재호출 없음.

```bat
:: .env 에 토큰 설정 후
python -m targeting_agent.scripts.probe_meta_api --login instagram
python -m targeting_agent.scripts.probe_meta_api --login facebook   :: business_discovery까지 확인
```

- 결과는 위 1장과 같은 표로 출력되고 `data/exports/phase11a_probe_<시각>.md`에 저장된다.
- 계정 ID/username 등 식별자는 기본 마스킹된다(`--no-redact`로 해제 가능).
- **Access Token은 출력·저장하지 않는다.**
- 공식 문서에 없는 endpoint는 추측 호출하지 않는다. 팔로워 관계는 호출 자체를 하지 않고 미지원으로 기록한다.

실행 후 산출된 표를 이 문서 1장에 덮어쓰고, 2장 판정을 GO / LIMITED GO / NO-GO 중 하나로 확정한다.

## 5. 이 결과가 v0.2에 주는 영향

| 항목 | 변경 |
| --- | --- |
| App Review | **전제에서 제외.** 내 계정 범위만 쓰면 Standard Access로 충분 |
| Phase 11B | Commenter Discovery(필수) + Profile Enrichment(선택)로 분리 |
| Phase 16B | Follow-back 자동 감지 제외 → Dashboard 수동 Feedback |
| Login 방식 | Enrichment가 필요하면 Facebook Login(+Page 연결), 아니면 Instagram Login이 더 단순 |
| Phase 12A | NO-GO 대비가 아니라 **기본 입력 경로**로 먼저 만든다(현 진행 순서와 일치) |
