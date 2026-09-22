"""Instagram 화면 selector registry (Phase 18A).

Instagram UI는 자주 바뀐다. selector를 여러 파일에 흩어 두지 않고 여기서만 관리한다.

우선순위: 안정적인 href 패턴 > 접근성 역할/라벨 > 최소 CSS.
nth-child처럼 깨지기 쉬운 selector는 쓰지 않는다.

여기 있는 것은 **공개 화면 DOM을 읽기 위한 selector**다.
내부 API/GraphQL endpoint를 호출하거나 network 응답을 가로채지 않는다.
"""
from __future__ import annotations

BASE_URL = "https://www.instagram.com"
# 검색 결과 화면. UI를 클릭으로 헤매지 않고 공개 URL 패턴을 그대로 연다.
SEARCH_URL = BASE_URL + "/explore/search/keyword/?q={query}"

# 게시물 링크(릴스 우선). 여러 후보를 순서대로 시도한다.
POST_LINK_SELECTORS = (
    'a[href*="/reel/"]',
    'main a[href*="/reel/"]',
    'a[href*="/p/"]',
)

# 상세 화면에서 작성자 이름
USERNAME_SELECTORS = (
    'header a[href^="/"][role="link"]',
    'header a[href^="/"]',
    'article header a[href^="/"]',
)

# 캡션 텍스트
CAPTION_SELECTORS = (
    "article h1",
    'article [data-testid="post-comment-root"] span',
    "article ul li span[dir='auto']",
    "article span[dir='auto']",
)

# 로그인 상태 판정용 마커
LOGGED_IN_MARKERS = (
    'svg[aria-label="홈"]',
    'svg[aria-label="Home"]',
    'a[href="/direct/inbox/"]',
    'nav a[href*="/explore/"]',
)
LOGIN_REQUIRED_MARKERS = (
    'input[name="username"]',
    'form#loginForm',
    'a[href="/accounts/login/"]',
)

# 화면 텍스트로 판정하는 중단 조건(정상 종료가 아니라 즉시 중단 대상)
CHALLENGE_TEXTS = (
    "challenge",
    "suspicious login",
    "confirm it's you",
    "본인 확인",
    "계정을 확인",
    "보안 코드",
)
WARNING_TEXTS = (
    "action blocked",
    "try again later",
    "temporarily restricted",
    "we restrict certain activity",
    "일시적으로 제한",
    "나중에 다시 시도",
    "차단되었습니다",
)
LOGIN_TEXTS = (
    "log in to instagram",
    "로그인하여",
    "instagram에 로그인",
)
