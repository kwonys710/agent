from __future__ import annotations

from dailyreels.subtitles import (
    CaptionStyle,
    body_text,
    build_ass,
    escape_ass_text,
    format_timestamp,
    hex_to_ass,
    hook_text,
)

STYLE = CaptionStyle.from_config({"bottom_margin": 340, "side_margin": 90})


def test_escape_keeps_korean_and_neutralises_control_chars():
    assert escape_ass_text("이른 아침 이동 중.") == "이른 아침 이동 중."
    assert escape_ass_text("철판에 골고루\n볶는 밥.") == r"철판에 골고루\N볶는 밥."
    assert escape_ass_text("a{b}c") == "a(b)c"
    assert escape_ass_text(" 퇴근 \r\n 실패 ") == r"퇴근\N실패"


def test_body_text_puts_a_smaller_time_above_the_caption():
    text = body_text("07:50", "이른 아침 이동 중.", STYLE)
    assert text == r"{\fs44}07:50\N{\fs64}이른 아침 이동 중."


def test_body_text_handles_missing_parts():
    assert body_text("", "퇴근 실패.", STYLE) == r"{\fs64}퇴근 실패."
    assert body_text("18:01", "", STYLE) == r"{\fs44}18:01"
    assert body_text("", "", STYLE) == ""


def test_hook_text_is_large_and_capped_at_two_lines():
    text = hook_text("철판에 골고루\n볶는 밥.", STYLE)
    assert text.startswith(r"{\fs92}")
    assert text.count(r"\N") == 1

    three = hook_text("한\n두\n세", STYLE)
    assert three.count(r"\N") == 1  # planner wording kept, just re-flowed
    assert "세" in three


def test_hex_to_ass_is_bgr_with_inverted_alpha():
    assert hex_to_ass("#FFFFFF") == "&H00FFFFFF"
    assert hex_to_ass("#000000") == "&H00000000"
    assert hex_to_ass("#FF0000") == "&H000000FF"
    assert hex_to_ass("#000000", alpha=1.0) == "&HFF000000"


def test_format_timestamp():
    assert format_timestamp(0) == "0:00:00.00"
    assert format_timestamp(2.5) == "0:00:02.50"
    assert format_timestamp(-1) == "0:00:00.00"


def test_build_ass_has_playres_styles_and_one_event():
    content = build_ass(
        body_text("07:50", "이른 아침 이동 중.", STYLE), 2.0, STYLE, "Paperlogy"
    )
    assert "PlayResX: 1080" in content and "PlayResY: 1920" in content
    assert "Style: Body,Paperlogy," in content
    assert ",2,90,90,340,1" in content        # bottom-centre inside the safe area
    assert "Style: Hook," in content and ",5,90,90,0,1" in content
    assert content.count("Dialogue:") == 1
    assert "0:00:02.00,Body" in content
    assert "이른 아침 이동 중." in content


def test_build_ass_hook_uses_the_hook_style():
    content = build_ass(hook_text("철판에 골고루\n볶는 밥.", STYLE), 2.0, STYLE, "Paperlogy", is_hook=True)
    assert ",Hook,," in content


def test_build_ass_without_text_has_no_events():
    assert build_ass("", 2.0, STYLE, "Paperlogy").count("Dialogue:") == 0
