"""changes.list의 pagination 루프(paginate_changes)를 실제 googleapiclient 없이 검증한다.

GoogleDriveClient.list_changes는 내부적으로 이 함수를 그대로 사용하므로,
여기서 순수 함수로 검증하면 실제 다중 페이지 API 응답에서도 동일하게 동작함을 보장한다.
"""
from engine.drive.client import paginate_changes


def test_paginate_changes_collects_all_pages_and_uses_last_start_token():
    pages = [
        {"changes": [{"fileId": "F1"}, {"fileId": "F2"}], "nextPageToken": "p2"},
        {"changes": [{"fileId": "F3"}], "nextPageToken": "p3"},
        {"changes": [{"fileId": "F4"}], "newStartPageToken": "FINAL"},
    ]
    calls = []

    def fetch_page(token):
        calls.append(token)
        return pages[len(calls) - 1]

    changes, new_start_page_token = paginate_changes(fetch_page, "p1")

    assert [c["fileId"] for c in changes] == ["F1", "F2", "F3", "F4"]
    assert new_start_page_token == "FINAL"
    # 중간 페이지 token(p2, p3)이 최종 checkpoint로 쓰이면 안 된다 — 오직 newStartPageToken만.
    assert new_start_page_token not in ("p2", "p3")
    assert calls == ["p1", "p2", "p3"]


def test_paginate_changes_single_page_stops_immediately():
    def fetch_page(token):
        assert token == "start"
        return {"changes": [{"fileId": "ONLY"}], "newStartPageToken": "next"}

    changes, new_start_page_token = paginate_changes(fetch_page, "start")

    assert [c["fileId"] for c in changes] == ["ONLY"]
    assert new_start_page_token == "next"


def test_paginate_changes_stops_when_next_page_token_absent_even_without_new_start_token():
    # 방어적 테스트: 실제 API는 마지막 페이지에 항상 newStartPageToken을 주지만,
    # nextPageToken이 없으면 그것만으로도 루프가 종료되어야 한다(무한루프 방지).
    def fetch_page(token):
        return {"changes": []}

    changes, new_start_page_token = paginate_changes(fetch_page, "start")

    assert changes == []
    assert new_start_page_token is None
