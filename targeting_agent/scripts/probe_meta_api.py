"""Phase 11A — Meta API Feasibility Probe (읽기 전용, 최소 범위).

목적: Phase 11(내 게시물 댓글 작성자 기반 Discovery)을 구현하기 전에
실제 계정 + 실제 API 응답으로 가능/불가를 확정한다.

수행하는 것:
  1. 내 계정 조회                      → 토큰/계정 유형 확인
  2. 내 미디어 목록(댓글 있는 것 우선)   → Reel 대상 확인
  3. 미디어 댓글 조회                   → id/text/timestamp/username/from 반환 여부
  4. 댓글 작성자 Profile 조회 시도       → Enrichment 가능 여부(business_discovery)
  5. 팔로워 관계                        → 공식 endpoint 부재이므로 호출하지 않고 미지원으로 기록

하지 않는 것:
  - 쓰기 동작(댓글/좋아요/숨김) 일절 없음
  - 문서에 없는 endpoint 추측 호출 없음
  - 실패한 호출 반복 재시도 없음(호출당 1회)
  - Access Token 출력/저장 없음 (결과 파일에도 남기지 않는다)

실행:
    python -m targeting_agent.scripts.probe_meta_api --login instagram
    python -m targeting_agent.scripts.probe_meta_api --login facebook --no-redact
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

if __package__ in (None, ""):  # pragma: no cover - 직접 실행 대응
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    __package__ = "targeting_agent.scripts"

from ..core.config import load_dotenv
from ..discovery.instagram_api import GRAPH_API_VERSION, load_credentials

INSTAGRAM_LOGIN_BASE = "https://graph.instagram.com"
FACEBOOK_LOGIN_BASE = f"https://graph.facebook.com/{GRAPH_API_VERSION}"

COMMENT_FIELDS = "id,text,timestamp,username,from{id,username},like_count,replies{id,text,username,from{id,username}}"
MEDIA_FIELDS = "id,media_type,media_product_type,permalink,comments_count,timestamp"

Transport = Callable[[str], dict]

PASS, FAIL, LIMITED, TBD, NOT_SUPPORTED = "PASS", "FAIL", "LIMITED", "TBD", "NOT_SUPPORTED"


@dataclass
class Check:
    name: str
    result: str = TBD
    evidence: str = ""
    impact: str = ""


@dataclass
class ProbeReport:
    login_type: str
    checks: list[Check] = field(default_factory=list)
    samples: dict[str, Any] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    def add(self, name: str, result: str, evidence: str = "", impact: str = "") -> Check:
        check = Check(name=name, result=result, evidence=evidence, impact=impact)
        self.checks.append(check)
        return check

    def verdict(self) -> str:
        by_name = {c.name: c.result for c in self.checks}
        if by_name.get("내 Reel 댓글 조회") != PASS:
            return "NO-GO"
        if by_name.get("comment username") != PASS:
            return "NO-GO"
        if by_name.get("Profile 조회") == PASS:
            return "GO"
        return "LIMITED GO"


def _get(url: str, timeout: int = 15) -> dict:
    """GET 한 번. 실패해도 재시도하지 않는다(quota 낭비 방지)."""
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            return json.loads(exc.read().decode("utf-8"))
        except (ValueError, OSError):
            return {"error": {"code": exc.code, "message": f"HTTP {exc.code}"}}
    except urllib.error.URLError as exc:
        return {"error": {"code": 0, "message": f"연결 실패: {exc.reason}"}}


def make_transport(base: str, token: str) -> Transport:
    """path?params 를 받아 호출하는 transport. 토큰은 여기서만 붙인다."""

    def transport(path_with_query: str) -> dict:
        separator = "&" if "?" in path_with_query else "?"
        url = f"{base}/{path_with_query.lstrip('/')}{separator}access_token={urllib.parse.quote(token)}"
        return _get(url)

    return transport


def _error_of(payload: dict) -> Optional[str]:
    error = payload.get("error")
    if not error:
        return None
    if isinstance(error, dict):
        return f"code={error.get('code')} {error.get('message')}"
    return str(error)


def _redact(value: Optional[str], enabled: bool) -> str:
    if not value:
        return ""
    if not enabled:
        return str(value)
    text = str(value)
    return text[0] + "*" * max(len(text) - 1, 1)


def run_probe(
    transport: Transport,
    *,
    login_type: str,
    user_id: str,
    redact: bool = True,
    media_limit: int = 10,
) -> ProbeReport:
    """실제 응답을 받아 검증 결과를 채운다."""
    report = ProbeReport(login_type=login_type)

    # 1. 내 계정
    me = transport(f"{user_id}?fields=id,username,account_type,media_count")
    error = _error_of(me)
    if error:
        report.add("내 계정 조회", FAIL, error, "토큰/권한 확인 필요 — 이후 검증 불가")
        report.errors.append(error)
        return report
    report.add(
        "내 계정 조회",
        PASS,
        f"account_type={me.get('account_type', 'n/a')}, media_count={me.get('media_count', 'n/a')}",
        "토큰 유효",
    )
    report.samples["me"] = {
        "id": _redact(me.get("id"), redact),
        "username": _redact(me.get("username"), redact),
        "account_type": me.get("account_type"),
    }

    # 2. 내 미디어
    media_payload = transport(f"{user_id}/media?fields={MEDIA_FIELDS}&limit={media_limit}")
    error = _error_of(media_payload)
    if error:
        report.add("내 미디어 조회", FAIL, error, "Discovery 불가")
        report.errors.append(error)
        return report
    media_items = [m for m in (media_payload.get("data") or []) if isinstance(m, dict)]
    reels = [m for m in media_items if str(m.get("media_product_type") or "").upper() == "REELS"]
    report.add(
        "내 미디어 조회",
        PASS if media_items else FAIL,
        f"media={len(media_items)}건 (REELS {len(reels)}건)",
        "댓글 조회 대상 확보" if media_items else "게시물이 없어 검증 불가",
    )
    if not media_items:
        return report

    # 3. 댓글 — 댓글 수가 많은 미디어부터 시도(호출 낭비 방지)
    targets = sorted(media_items, key=lambda m: int(m.get("comments_count") or 0), reverse=True)
    target = targets[0]
    if int(target.get("comments_count") or 0) <= 0:
        report.add("내 Reel 댓글 조회", TBD, "댓글이 달린 게시물이 없음", "댓글 있는 게시물로 재실행 필요")
        return report

    comments_payload = transport(f"{target['id']}/comments?fields={COMMENT_FIELDS}&limit=25")
    error = _error_of(comments_payload)
    if error:
        report.add("내 Reel 댓글 조회", FAIL, error, "Phase 11 불가 → Phase 12로 전환")
        report.errors.append(error)
        return report

    comments = [c for c in (comments_payload.get("data") or []) if isinstance(c, dict)]
    is_reel = str(target.get("media_product_type") or "").upper() == "REELS"
    report.add(
        "내 Reel 댓글 조회",
        PASS if comments else TBD,
        f"{'REEL' if is_reel else target.get('media_type')} 게시물에서 댓글 {len(comments)}건",
        "Commenter Discovery 가능" if comments else "댓글 표본 없음",
    )
    if not comments:
        return report

    with_username = [c for c in comments if c.get("username")]
    with_from = [c for c in comments if isinstance(c.get("from"), dict)]
    from_with_username = [c for c in with_from if c["from"].get("username")]
    from_with_id = [c for c in with_from if c["from"].get("id")]

    report.add(
        "comment username",
        PASS if with_username else (LIMITED if from_with_username else FAIL),
        f"username 필드 {len(with_username)}/{len(comments)}, from.username {len(from_with_username)}/{len(comments)}",
        "Creator 식별 가능" if (with_username or from_with_username) else "Creator 식별 불가",
    )
    report.add(
        "commenter id",
        PASS if from_with_id else FAIL,
        f"from.id {len(from_with_id)}/{len(comments)}",
        "중복 제거/추적 키로 사용 가능" if from_with_id else "username만으로 식별해야 함",
    )
    report.add(
        "Consumer commenter",
        PASS if len(with_username) == len(comments) else (LIMITED if with_username else FAIL),
        f"식별 가능한 댓글 {len(with_username)}/{len(comments)} — 일부 누락 시 Consumer 계정 제한 가능성",
        "누락분은 Candidate에서 제외",
    )
    report.samples["comments"] = [
        {
            "id": _redact(c.get("id"), redact),
            "username": _redact(c.get("username"), redact),
            "from_id": _redact((c.get("from") or {}).get("id"), redact),
            "from_username": _redact((c.get("from") or {}).get("username"), redact),
            "has_text": bool(c.get("text")),
            "has_timestamp": bool(c.get("timestamp")),
        }
        for c in comments[:5]
    ]

    # 4. Profile Enrichment — business_discovery는 Facebook Login + 대상이 Professional일 때만
    usernames = [str(c.get("username")) for c in comments if c.get("username")]
    if not usernames:
        report.add("Profile 조회", FAIL, "조회할 username 없음", "Enrichment 불가")
    elif login_type != "facebook":
        report.add(
            "Profile 조회",
            NOT_SUPPORTED,
            "business_discovery는 Facebook Login 전용(문서) — Instagram Login에서는 호출하지 않음",
            "Enrichment 분리 필요: username만으로 Candidate 생성",
        )
    else:
        sample_username = usernames[0]
        discovery = transport(
            f"{user_id}?fields=business_discovery.username({sample_username})"
            "{id,username,followers_count,media_count}"
        )
        error = _error_of(discovery)
        if error:
            report.add(
                "Profile 조회",
                LIMITED,
                f"business_discovery 실패: {error}",
                "Consumer/비공개 대상은 조회 불가 → Enrichment는 선택 단계로 분리",
            )
        else:
            node = discovery.get("business_discovery") or {}
            report.add(
                "Profile 조회",
                PASS if node.get("followers_count") is not None else LIMITED,
                f"followers_count={node.get('followers_count')}, media_count={node.get('media_count')}",
                "팔로워 기준 Creator Fit 적용 가능",
            )

    # 5. 팔로워 관계 — 공식 endpoint가 없으므로 호출하지 않는다
    report.add(
        "follower relationship",
        NOT_SUPPORTED,
        "공식 API에 follower/following 목록 또는 관계 확인 endpoint 없음(문서 기준) — 호출 시도하지 않음",
        "Phase 16B는 Dashboard 수동 Feedback으로 유지",
    )
    return report


def format_report(report: ProbeReport) -> str:
    """요구된 검증 표 형식으로 출력한다."""
    lines = [
        f"# Phase 11A Probe 결과 ({report.login_type} login)",
        "",
        f"- 실행 시각: {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
        f"- 판정: **{report.verdict()}**",
        "",
        "| 검증항목 | 결과 | 실제 응답/근거 | v0.2 영향 |",
        "| --- | --- | --- | --- |",
    ]
    for check in report.checks:
        lines.append(f"| {check.name} | {check.result} | {check.evidence} | {check.impact} |")
    if report.errors:
        lines += ["", "## 오류", *[f"- {e}" for e in report.errors]]
    if report.samples:
        lines += [
            "",
            "## 응답 표본(식별자 마스킹)",
            "```json",
            json.dumps(report.samples, ensure_ascii=False, indent=2),
            "```",
        ]
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Phase 11A Meta API Feasibility Probe")
    parser.add_argument(
        "--login",
        choices=("instagram", "facebook"),
        default="instagram",
        help="Instagram Login(graph.instagram.com) | Facebook Login(graph.facebook.com)",
    )
    parser.add_argument("--user-id", default=None, help="기본값: IG_BUSINESS_ACCOUNT_ID 또는 'me'")
    parser.add_argument("--media-limit", type=int, default=10)
    parser.add_argument("--no-redact", action="store_true", help="식별자 마스킹 해제")
    parser.add_argument("--out", type=Path, default=None, help="결과 Markdown 저장 경로")
    args = parser.parse_args(argv)

    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
    credentials = load_credentials()
    if not credentials.access_token:
        print("[오류] IG_ACCESS_TOKEN이 없습니다. .env에 설정하세요.", file=sys.stderr)
        return 2

    base = INSTAGRAM_LOGIN_BASE if args.login == "instagram" else FACEBOOK_LOGIN_BASE
    user_id = args.user_id or credentials.business_account_id or "me"
    if args.login == "facebook" and user_id == "me":
        print(
            "[오류] Facebook Login은 IG_BUSINESS_ACCOUNT_ID가 필요합니다.", file=sys.stderr
        )
        return 2

    report = run_probe(
        make_transport(base, credentials.access_token),
        login_type=args.login,
        user_id=user_id,
        redact=not args.no_redact,
        media_limit=args.media_limit,
    )
    text = format_report(report)
    print(text)

    out = args.out
    if out is None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        out = Path(__file__).resolve().parents[1] / "data" / "exports" / f"phase11a_probe_{stamp}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    print(f"\n결과 저장: {out}")
    return 0 if report.verdict() != "NO-GO" else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
