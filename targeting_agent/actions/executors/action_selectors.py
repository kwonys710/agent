"""Action(쓰기) 경로 selector registry (Phase 18B).

Discovery(읽기) selector와 **파일을 분리**한다. 읽기 경로에는 쓰기 selector가
한 줄도 들어가지 않아야 하기 때문이다(테스트가 이를 검사한다).

여기 있는 동작은 **LIKE / COMMENT 두 가지뿐**이다.
FOLLOW / DM / SAVE / SHARE selector는 두지 않는다 — 기능 자체를 만들지 않는다.

우선순위는 Discovery와 같다: aria-label / role → 최소 CSS. 해시 class는 쓰지 않는다.
한국어 UI가 기본이므로 라벨은 한국어 + 영어만 둔다.
"""
from __future__ import annotations

Entry = tuple[str, str]

# --- LIKE ----------------------------------------------------------------
# 아직 좋아요하지 않은 상태의 버튼
LIKE_BUTTON: tuple[Entry, ...] = (
    ("like_aria_ko", '[role="button"]:has(svg[aria-label="좋아요"])'),
    ("like_aria_en", '[role="button"]:has(svg[aria-label="Like"])'),
    ("like_svg_ko", 'svg[aria-label="좋아요"]'),
    ("like_svg_en", 'svg[aria-label="Like"]'),
    ("like_role_ko", '[role="button"][aria-label="좋아요"]'),
    ("like_role_en", '[role="button"][aria-label="Like"]'),
)
# 이미 좋아요한 상태(= 취소 버튼이 보인다). 이 경우 다시 누르지 않는다.
ALREADY_LIKED: tuple[Entry, ...] = (
    ("unlike_aria_ko", 'svg[aria-label="좋아요 취소"]'),
    ("unlike_aria_en", 'svg[aria-label="Unlike"]'),
    ("unlike_role_ko", '[role="button"][aria-label="좋아요 취소"]'),
    ("unlike_role_en", '[role="button"][aria-label="Unlike"]'),
)

# --- COMMENT -------------------------------------------------------------
COMMENT_INPUT: tuple[Entry, ...] = (
    ("comment_textarea_aria_ko", 'textarea[aria-label^="댓글"]'),
    ("comment_textarea_aria_en", 'textarea[aria-label^="Add a comment"]'),
    ("comment_textarea_placeholder_ko", 'textarea[placeholder^="댓글"]'),
    ("comment_textarea_placeholder_en", 'textarea[placeholder^="Add a comment"]'),
    ("comment_textarea_any", "form textarea"),
)
COMMENT_SUBMIT: tuple[Entry, ...] = (
    ("submit_role_ko", '[role="button"]:text-is("게시")'),
    ("submit_role_en", '[role="button"]:text-is("Post")'),
    ("submit_button_ko", 'button:text-is("게시")'),
    ("submit_button_en", 'button:text-is("Post")'),
    ("submit_type", 'form button[type="submit"]'),
)

# --- 중단 조건 -----------------------------------------------------------
# 화면에 아래 문구가 보이면 Action Run 전체를 즉시 중단한다(우회하지 않는다).
ACTION_BLOCKED_TEXTS = (
    "action blocked",
    "we restrict certain activity",
    "try again later",
    "작업이 차단",
    "차단되었습니다",
    "나중에 다시 시도",
    "일시적으로 제한",
)


def keys_of(entries: tuple[Entry, ...]) -> tuple[str, ...]:
    return tuple(key for key, _ in entries)
