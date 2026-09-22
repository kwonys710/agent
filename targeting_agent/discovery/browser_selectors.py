"""Instagram 화면 selector registry (Phase 18A / 18A.1).

Instagram UI는 자주 바뀐다. selector를 여러 파일에 흩어 두지 않고 **여기서만** 관리한다.

우선순위(18A.1):
  1) 접근성 역할·라벨(aria-label / placeholder / role)
  2) 안정적인 href 패턴(`/reel/`, `/explore/tags/`)
  3) 최소한의 CSS
`nth-child`, 절대 경로 CSS chain, 해시 class(React 내부 class)는 쓰지 않는다.

한국어 UI가 기본 운영 환경이므로 라벨은 **한국어 + 영어 두 가지만** 둔다
(전체 언어 목록을 넣지 않는다).

여기 있는 것은 **공개 화면 DOM을 읽기 위한 selector**다.
내부 API/GraphQL endpoint를 호출하거나 network 응답을 가로채지 않는다.
"""
from __future__ import annotations

BASE_URL = "https://www.instagram.com"
HOME_URL = BASE_URL + "/"

# selector 후보는 (key, selector) 순서쌍으로 둔다.
# key는 실패 로그/스크린샷 파일명에 그대로 쓰여서 "어느 selector가 안 맞는지"를 알려 준다.
Entry = tuple[str, str]

# --- 1) 검색 진입 --------------------------------------------------------
# 공개 Web UI의 네비게이션만 사용한다(비공개 endpoint를 추측해 부르지 않는다).
SEARCH_ENTRY: tuple[Entry, ...] = (
    ("nav_search_aria_ko", '[role="link"]:has(svg[aria-label="검색"])'),
    ("nav_search_aria_en", '[role="link"]:has(svg[aria-label="Search"])'),
    ("nav_search_button_ko", 'button:has(svg[aria-label="검색"])'),
    ("nav_search_button_en", 'button:has(svg[aria-label="Search"])'),
    ("nav_search_svg_ko", 'svg[aria-label="검색"]'),
    ("nav_search_svg_en", 'svg[aria-label="Search"]'),
    ("nav_search_href", 'a[href^="/explore/search"]'),
)

# --- 2) 검색 입력 --------------------------------------------------------
SEARCH_INPUT: tuple[Entry, ...] = (
    ("input_placeholder_ko", 'input[placeholder="검색"]'),
    ("input_placeholder_en", 'input[placeholder="Search"]'),
    ("input_aria_ko", 'input[aria-label="검색 입력"]'),
    ("input_aria_en", 'input[aria-label="Search input"]'),
    ("input_role_searchbox", '[role="searchbox"]'),
    ("input_search_type", 'input[type="search"]'),
)

# --- 3) 검색 결과(드롭다운) ----------------------------------------------
# 해시태그 결과를 우선한다 — 공개 태그 페이지가 Reel을 가장 많이 보여 준다.
SEARCH_RESULT_HASHTAG: tuple[Entry, ...] = (
    ("result_hashtag_href", 'a[href^="/explore/tags/"]'),
)
# 해시태그 결과가 없을 때만 계정 결과로 넘어간다(탐색 depth는 여기까지).
SEARCH_RESULT_ACCOUNT: tuple[Entry, ...] = (
    ("result_account_link", '[role="none"] a[href^="/"]:not([href*="/explore/"])'),
    ("result_account_any", 'a[href^="/"][role="link"]:not([href*="/explore/"])'),
)

# --- 4) 결과 페이지(태그/프로필)에 도착했는지 ------------------------------
RESULT_PAGE_MARKERS: tuple[Entry, ...] = (
    ("result_page_reel_link", 'a[href*="/reel/"]'),
    ("result_page_post_link", 'main a[href*="/p/"]'),
    ("result_page_main", "main article"),
)

# --- 5) Reel 링크 --------------------------------------------------------
POST_LINK_SELECTORS: tuple[Entry, ...] = (
    ("reel_href", 'a[href*="/reel/"]'),
    ("reel_href_main", 'main a[href*="/reel/"]'),
)

# --- 6) 상세 화면 --------------------------------------------------------
USERNAME_SELECTORS: tuple[Entry, ...] = (
    ("username_header_link", 'header a[href^="/"][role="link"]'),
    ("username_article_header", "article header a[href^=\"/\"]"),
    ("username_dialog_header", '[role="dialog"] header a[href^="/"]'),
    ("username_main_link", 'main header a[href^="/"]'),
)
CAPTION_SELECTORS: tuple[Entry, ...] = (
    ("caption_h1", "article h1"),
    ("caption_dialog_h1", '[role="dialog"] h1'),
    ("caption_page_h1", "main h1"),
    ("caption_article_span", 'article span[dir="auto"]'),
    ("caption_dialog_span", '[role="dialog"] span[dir="auto"]'),
)

# --- 세션 상태 판정 --------------------------------------------------------
LOGGED_IN_MARKERS = (
    'svg[aria-label="홈"]',
    'svg[aria-label="Home"]',
    'a[href="/direct/inbox/"]',
    'nav a[href*="/explore/"]',
)
LOGIN_REQUIRED_MARKERS = (
    'input[name="username"]',
    "form#loginForm",
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


def keys_of(entries: tuple[Entry, ...]) -> tuple[str, ...]:
    """실패 로그에 남길 selector 키 목록."""
    return tuple(key for key, _ in entries)
