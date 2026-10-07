"""제한된 DOM 진단 (Phase 18A.2).

selector가 안 맞을 때 "현재 화면이 어떻게 생겼는지"를 **구조화된 최소 정보**로만 남긴다.

수집하는 것(공개 화면 구조):
    현재 URL / 페이지 title / 보이는 textbox의 placeholder·aria-label·role
    navigation href 샘플 / `/reel/` 링크 수 / main·dialog 존재 여부

수집하지 않는 것:
    전체 HTML, 쿠키, 세션 토큰, localStorage, 사용자 콘텐츠 본문
    (화면 텍스트를 그대로 긁어 오지 않는다 — 구조만 본다)

출력은 로그 1줄 JSON이다. 길이를 제한해 로그가 넘치지 않게 한다.
"""
from __future__ import annotations

import json
from typing import Any, Optional

from ..core.logger import get_logger

logger = get_logger("discovery.diagnostics")

MAX_ITEMS = 8  # textbox / href 샘플 상한

# 진단용 질의도 접근성 속성 위주로 둔다(해시 class를 쓰지 않는다).
TEXTBOX_SELECTOR = 'input:not([type="hidden"]), textarea, [role="searchbox"], [role="textbox"]'
NAV_LINK_SELECTOR = "nav a[href], header a[href]"
REEL_LINK_SELECTOR = 'a[href*="/reel/"]'


def collect_diagnostics(page: Any, stage: str, *, max_items: int = MAX_ITEMS) -> dict[str, Any]:
    """현재 화면의 구조만 요약한다. 실패해도 예외를 올리지 않는다."""
    info: dict[str, Any] = {"stage": stage}
    info["url"] = _safe(lambda: str(page.url), "")
    info["title"] = _safe(lambda: str(page.title() or "")[:120], "")
    info["textboxes"] = _safe(lambda: _textboxes(page, max_items), [])
    info["nav_hrefs"] = _safe(lambda: _nav_hrefs(page, max_items), [])
    info["reel_href_count"] = _safe(lambda: int(page.locator(REEL_LINK_SELECTOR).count()), 0)
    info["has_main"] = _safe(lambda: bool(page.locator("main").count()), False)
    info["has_dialog"] = _safe(lambda: bool(page.locator('[role="dialog"]').count()), False)
    return info


def log_diagnostics(page: Any, stage: str, *, max_items: int = MAX_ITEMS) -> dict[str, Any]:
    """진단 결과를 JSON 한 줄로 남기고 그대로 돌려준다."""
    info = collect_diagnostics(page, stage, max_items=max_items)
    logger.warning("DOM 진단 %s", json.dumps(info, ensure_ascii=False))
    return info


# --- 내부 ------------------------------------------------------------------
def _textboxes(page: Any, max_items: int) -> list[dict[str, Optional[str]]]:
    """보이는 입력 필드의 접근성 속성만 모은다(입력된 값은 읽지 않는다)."""
    found = page.locator(TEXTBOX_SELECTOR)
    items: list[dict[str, Optional[str]]] = []
    for index in range(min(found.count(), max_items)):
        node = found.nth(index)
        try:
            if not node.is_visible():
                continue
            items.append(
                {
                    "placeholder": node.get_attribute("placeholder"),
                    "aria_label": node.get_attribute("aria-label"),
                    "role": node.get_attribute("role"),
                    "type": node.get_attribute("type"),
                }
            )
        except Exception:  # noqa: BLE001 - 하나 실패가 진단 전체를 막지 않는다
            continue
    return items


def _nav_hrefs(page: Any, max_items: int) -> list[str]:
    """navigation 링크 경로만 모은다(쿼리는 버린다 — 식별 정보가 섞이지 않게)."""
    try:
        values = page.locator(NAV_LINK_SELECTOR).evaluate_all(
            "nodes => nodes.map(n => n.getAttribute('href'))"
        )
    except Exception:  # noqa: BLE001
        return []
    paths: list[str] = []
    for href in values or []:
        if not href:
            continue
        path = str(href).split("?")[0][:80]
        if path not in paths:
            paths.append(path)
        if len(paths) >= max_items:
            break
    return paths


def _safe(getter: Any, fallback: Any) -> Any:
    try:
        return getter()
    except Exception:  # noqa: BLE001 - 진단은 실패해도 Run을 막지 않는다
        return fallback
