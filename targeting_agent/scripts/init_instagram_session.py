"""Instagram 로그인 세션 준비 (Phase 18A).

브라우저를 띄우고 **운영자가 직접** Instagram에 로그인하도록 한다.
로그인 상태는 Playwright persistent profile(기본 `data/browser_profile/`)에만 남는다.

이 스크립트가 하지 않는 것:
- ID/PW를 묻거나 저장하거나 자동 입력하지 않는다(코드/설정/DB/로그 어디에도 없다).
- 쿠키·세션 토큰을 읽거나 파일로 빼내지 않는다.
- 2FA / Challenge / CAPTCHA를 대신 처리하지 않는다 — 운영자가 화면에서 직접 한다.
- 좋아요·댓글·팔로우 등 어떤 쓰기 동작도 하지 않는다.

사용:
    python -m targeting_agent.scripts.init_instagram_session
    python -m targeting_agent.scripts.init_instagram_session --check
    python -m targeting_agent.scripts.init_instagram_session --reset
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path
from typing import Optional, Sequence

from ..core.config import load_config
from ..discovery.browser_models import SessionState

BASE_URL = "https://www.instagram.com"


def _profile_dir(config) -> Path:
    return config._resolve_path(
        config.get("browser_discovery.profile_dir", "data/browser_profile")
    )


def reset_profile(profile_dir: Path, *, assume_yes: bool = False) -> bool:
    """로그인 세션 프로필을 삭제한다. 반드시 명시적 확인을 받는다."""
    if not profile_dir.exists():
        print(f"[정보] 삭제할 세션이 없습니다: {profile_dir}")
        return False
    if not assume_yes:
        print(f"[확인] 브라우저 로그인 세션을 삭제합니다: {profile_dir}")
        answer = input("정말 삭제하려면 'DELETE' 를 입력하세요: ").strip()
        if answer != "DELETE":
            print("[취소] 아무 것도 삭제하지 않았습니다.")
            return False
    shutil.rmtree(profile_dir)
    print(f"[완료] 세션을 삭제했습니다: {profile_dir}")
    return True


def check_session(config) -> SessionState:
    """현재 프로필의 로그인 상태만 확인하고 종료한다(검색하지 않는다)."""
    from ..discovery.browser_instagram import PlaywrightBrowser

    browser = PlaywrightBrowser(
        profile_dir=_profile_dir(config),
        headless=bool(config.get("browser_discovery.headless", False)),
        timeout_ms=int(config.get("browser_discovery.timeout_ms", 20000)),
    )
    browser.start()
    try:
        return browser.session_state()
    finally:
        browser.close()


def open_for_login(config) -> int:
    """브라우저를 열어 두고, 운영자가 로그인을 마칠 때까지 기다린다."""
    from ..discovery.browser_instagram import PlaywrightBrowser

    profile_dir = _profile_dir(config)
    browser = PlaywrightBrowser(
        profile_dir=profile_dir,
        headless=False,  # 로그인은 사람이 직접 해야 하므로 항상 보이는 창으로 연다.
        timeout_ms=int(config.get("browser_discovery.timeout_ms", 20000)),
    )
    browser.start()
    try:
        browser.goto(BASE_URL + "/")
        print("=" * 60)
        print(" Instagram 로그인 창을 열었습니다.")
        print(" 1) 브라우저에서 직접 로그인하세요(ID/PW는 저장하지 않습니다).")
        print(" 2) 2단계 인증/본인 확인이 나오면 화면에서 직접 처리하세요.")
        print(" 3) 홈 피드가 보이면 이 창으로 돌아와 Enter 를 누르세요.")
        print(f" 세션 저장 위치: {profile_dir}")
        print("=" * 60)
        try:
            input("로그인을 마쳤으면 Enter: ")
        except EOFError:  # pragma: no cover - 비대화형 실행
            print("[경고] 대화형 입력이 없어 상태 확인으로 넘어갑니다.")
        state = browser.session_state()
        print(f"[세션 상태] {state.value}")
        if state is SessionState.LOGGED_IN:
            print("[완료] 이제 run_targeting_discovery.bat 을 실행할 수 있습니다.")
            return 0
        print("[미완료] 로그인이 확인되지 않았습니다. 다시 실행해 주세요.")
        return 1
    finally:
        browser.close()


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Instagram 로그인 세션 준비(운영자 직접 로그인)")
    parser.add_argument("--config", type=Path, default=None, help="config.yaml 경로")
    parser.add_argument("--check", action="store_true", help="로그인 상태만 확인하고 종료")
    parser.add_argument("--reset", action="store_true", help="저장된 로그인 세션을 삭제(확인 필요)")
    parser.add_argument("--yes", action="store_true", help="--reset 확인 프롬프트를 건너뛴다")
    args = parser.parse_args(argv)

    config = load_config(args.config)

    if args.reset:
        reset_profile(_profile_dir(config), assume_yes=args.yes)
        return 0

    try:
        if args.check:
            state = check_session(config)
            print(f"[세션 상태] {state.value}")
            return 0 if state is SessionState.LOGGED_IN else 1
        return open_for_login(config)
    except Exception as exc:  # noqa: BLE001 - 설치 누락 등을 사람이 읽을 수 있게
        print(f"[오류] {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
