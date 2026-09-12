from __future__ import annotations

from dataclasses import dataclass
from typing import Any

ASS_HEADER = """[Script Info]
ScriptType: v4.00+
WrapStyle: 2
ScaledBorderAndShadow: yes
YCbCr Matrix: None
PlayResX: {width}
PlayResY: {height}

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Body,{font},{body_size},{primary},{primary},{outline_colour},{back_colour},1,0,0,0,100,100,0,0,{border_style},{outline},{shadow},2,{side},{side},{bottom},1
Style: Hook,{font},{hook_size},{primary},{primary},{outline_colour},{back_colour},1,0,0,0,100,100,0,0,{border_style},{hook_outline},{shadow},5,{side},{side},0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

DEFAULTS: dict[str, Any] = {
    "body_font_size": 64,
    "time_font_size": 44,
    "hook_font_size": 92,
    "hook_max_lines": 2,
    "color": "#FFFFFF",
    "outline_color": "#000000",
    "outline": 4,
    "shadow": 2,
    "hook_outline": 5,
    "box": False,
    "box_opacity": 0.45,
    "bottom_margin": 340,
    "side_margin": 90,
    "time_format": "%H:%M",
}


@dataclass(frozen=True)
class CaptionStyle:
    values: dict[str, Any]

    def get(self, key: str) -> Any:
        return self.values.get(key, DEFAULTS[key])

    @classmethod
    def from_config(cls, config: dict[str, Any] | None) -> "CaptionStyle":
        return cls(values=dict(config or {}))


def hex_to_ass(color: str, alpha: float = 0.0) -> str:
    """#RRGGBB -> &HAABBGGRR (ASS alpha is inverted: 00 opaque)."""
    text = str(color).lstrip("#").strip()
    if len(text) == 3:
        text = "".join(char * 2 for char in text)
    if len(text) != 6:
        text = "FFFFFF"
    red, green, blue = text[0:2], text[2:4], text[4:6]
    alpha_byte = max(0, min(255, round(alpha * 255)))
    return f"&H{alpha_byte:02X}{blue}{green}{red}".upper()


def escape_ass_text(text: str) -> str:
    """Keep Korean text intact; only neutralise ASS control characters."""
    cleaned = str(text).replace("\r\n", "\n").replace("\r", "\n")
    cleaned = cleaned.replace("{", "(").replace("}", ")").replace("\\", "/")
    lines = [line.strip() for line in cleaned.split("\n")]
    return r"\N".join(line for line in lines if line)


def format_timestamp(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    hours, rest = divmod(int(seconds), 3600)
    minutes, secs = divmod(rest, 60)
    centis = int(round((seconds - int(seconds)) * 100))
    if centis == 100:  # rounding carry
        centis = 99
    return f"{hours:d}:{minutes:02d}:{secs:02d}.{centis:02d}"


def body_text(time_label: str, caption: str, style: CaptionStyle) -> str:
    """Two lines: the small time label above the caption."""
    caption_text = escape_ass_text(caption)
    label = escape_ass_text(time_label)
    body_size = style.get("body_font_size")
    if not label:
        return f"{{\\fs{body_size}}}{caption_text}" if caption_text else ""
    time_size = style.get("time_font_size")
    if not caption_text:
        return f"{{\\fs{time_size}}}{label}"
    return f"{{\\fs{time_size}}}{label}\\N{{\\fs{body_size}}}{caption_text}"


def hook_text(caption: str, style: CaptionStyle) -> str:
    text = escape_ass_text(caption)
    if not text:
        return ""
    max_lines = int(style.get("hook_max_lines"))
    lines = text.split(r"\N")
    if len(lines) > max_lines:  # keep the planner's wording, just re-flow it
        head = lines[: max_lines - 1]
        head.append(" ".join(lines[max_lines - 1 :]))
        text = r"\N".join(head)
    return f"{{\\fs{style.get('hook_font_size')}}}{text}"


def build_ass(
    text: str,
    duration: float,
    style: CaptionStyle,
    font_name: str,
    width: int = 1080,
    height: int = 1920,
    is_hook: bool = False,
) -> str:
    """One ASS file per rendered segment; timestamps start at zero."""
    back_alpha = 1.0 - float(style.get("box_opacity")) if style.get("box") else 1.0
    header = ASS_HEADER.format(
        width=width,
        height=height,
        font=font_name,
        body_size=style.get("body_font_size"),
        hook_size=style.get("hook_font_size"),
        primary=hex_to_ass(style.get("color")),
        outline_colour=hex_to_ass(style.get("outline_color")),
        back_colour=hex_to_ass(style.get("outline_color"), alpha=back_alpha),
        border_style=4 if style.get("box") else 1,
        outline=style.get("outline"),
        hook_outline=style.get("hook_outline"),
        shadow=style.get("shadow"),
        side=style.get("side_margin"),
        bottom=style.get("bottom_margin"),
    )
    if not text:
        return header
    event = (
        f"Dialogue: 0,{format_timestamp(0)},{format_timestamp(duration)},"
        f"{'Hook' if is_hook else 'Body'},,0,0,0,,{text}\n"
    )
    return header + event
