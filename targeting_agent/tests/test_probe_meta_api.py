"""Phase 11A Probe 로직 테스트(네트워크 호출 없이 응답을 주입해 검증)."""
from __future__ import annotations

from targeting_agent.scripts.probe_meta_api import format_report, run_probe

ME = {"id": "178414", "username": "my_account", "account_type": "BUSINESS", "media_count": 12}
MEDIA = {
    "data": [
        {"id": "MEDIA1", "media_type": "VIDEO", "media_product_type": "REELS",
         "permalink": "https://www.instagram.com/reel/MEDIA1/", "comments_count": 3},
        {"id": "MEDIA2", "media_type": "IMAGE", "media_product_type": "FEED", "comments_count": 0},
    ]
}
COMMENTS = {
    "data": [
        {"id": "C1", "text": "좋아요", "timestamp": "2026-09-20T10:00:00+0000",
         "username": "viewer_a", "from": {"id": "IGSID_A", "username": "viewer_a"}},
        {"id": "C2", "text": "공감돼요", "timestamp": "2026-09-20T11:00:00+0000",
         "username": "viewer_b", "from": {"id": "IGSID_B", "username": "viewer_b"}},
    ]
}


class FakeTransport:
    def __init__(self, responses: dict[str, dict]):
        self.responses = responses
        self.calls: list[str] = []

    def __call__(self, path: str) -> dict:
        self.calls.append(path)
        for key, value in self.responses.items():
            if key in path:
                return value
        return {"data": []}


def _results(report) -> dict[str, str]:
    return {check.name: check.result for check in report.checks}


def test_full_pass_path_with_facebook_login() -> None:
    transport = FakeTransport(
        {
            "/comments": COMMENTS,
            "/media": MEDIA,
            "business_discovery": {
                "business_discovery": {"id": "X", "username": "viewer_a", "followers_count": 4200, "media_count": 80}
            },
            "fields=id,username,account_type": ME,
        }
    )
    report = run_probe(transport, login_type="facebook", user_id="178414")
    results = _results(report)

    assert results["내 Reel 댓글 조회"] == "PASS"
    assert results["comment username"] == "PASS"
    assert results["commenter id"] == "PASS"
    assert results["Profile 조회"] == "PASS"
    assert results["follower relationship"] == "NOT_SUPPORTED"
    assert report.verdict() == "GO"


def test_instagram_login_skips_business_discovery() -> None:
    transport = FakeTransport(
        {"/comments": COMMENTS, "/media": MEDIA, "fields=id,username,account_type": ME}
    )
    report = run_probe(transport, login_type="instagram", user_id="me")

    assert _results(report)["Profile 조회"] == "NOT_SUPPORTED"
    assert report.verdict() == "LIMITED GO"   # Discovery는 되고 Enrichment만 불가
    assert not any("business_discovery" in call for call in transport.calls)


def test_comment_error_is_no_go() -> None:
    transport = FakeTransport(
        {
            "/comments": {"error": {"code": 10, "message": "permission denied"}},
            "/media": MEDIA,
            "fields=id,username,account_type": ME,
        }
    )
    report = run_probe(transport, login_type="instagram", user_id="me")

    assert report.verdict() == "NO-GO"
    assert report.errors and "permission denied" in report.errors[0]


def test_missing_username_is_limited() -> None:
    partial = {"data": [COMMENTS["data"][0], {"id": "C3", "text": "익명", "from": {"id": "IGSID_C"}}]}
    transport = FakeTransport(
        {"/comments": partial, "/media": MEDIA, "fields=id,username,account_type": ME}
    )
    report = run_probe(transport, login_type="instagram", user_id="me")
    results = _results(report)

    assert results["comment username"] == "PASS"      # 일부라도 username이 있으면 식별 가능
    assert results["Consumer commenter"] == "LIMITED"  # 전부는 아님 → 제한으로 표시


def test_report_redacts_identifiers_and_never_prints_token() -> None:
    transport = FakeTransport(
        {"/comments": COMMENTS, "/media": MEDIA, "fields=id,username,account_type": ME}
    )
    report = run_probe(transport, login_type="instagram", user_id="me", redact=True)
    text = format_report(report)

    assert "viewer_a" not in text and "IGSID_A" not in text
    assert "v*" in text  # 마스킹된 형태
    assert "access_token" not in text
    assert "| 검증항목 | 결과 |" in text


def test_no_retry_on_failure() -> None:
    transport = FakeTransport({"fields=id,username,account_type": {"error": {"code": 190, "message": "expired"}}})
    report = run_probe(transport, login_type="instagram", user_id="me")

    assert len(transport.calls) == 1      # 실패한 호출을 반복하지 않는다
    assert report.verdict() == "NO-GO"
