"""Comment Quality Calibration (Phase 18D.3).

실제로 만들어진 댓글이 **그 게시물의 내용에 근거했는지**를 본다.
Claude를 다시 부르지 않는다 — 이미 DB에 저장된 댓글만 읽어서 센다.

품질 게이트(comments/quality_filter.py)는 이미 구체적인 거절 사유를 남긴다
('generic:잘 보고', 'duplicate(0.89)', 'too_many_emoji(2)', 'no_context_keyword').
여기서는 그 사유를 **종류별로 묶어** 세고, 통과한 댓글이 실제로 caption에
근거했는지 따로 확인한다 — 게이트를 통과했다고 해서 내용이 있는 건 아니다.
"""
from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from typing import Optional

from ..core.config import Config

# 토큰은 한글 2자 이상 / 영문 3자 이상만 본다. 조사·짧은 단어는 우연히 겹친다.
_TOKEN_RE = re.compile(r"[가-힣]{2,}|[A-Za-z]{3,}")
_EMOJI_RE = re.compile(
    "[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF✀-➿]"
)
# 한국어 댓글의 흔한 어미. 겹침을 셀 때 빼지 않으면 전부 '근거 있음'이 된다.
_STOPWORDS = frozenset(
    {
        "너무", "진짜", "정말", "요즘", "그냥", "이거", "저도", "하는", "하고", "해서",
        "같아요", "네요", "어요", "아요", "습니다", "거의", "조금", "많이", "다시",
    }
)

# 충분한 표본이 있어야 경고한다(3건 중 1건 실패로 경고를 내지 않는다).
MIN_SAMPLES_FOR_WARNING = 10
# 아래 비율을 넘으면 Prompt 손볼 근거가 된다(§25).
REJECT_RATE_WARNING = 0.50
GENERIC_RATE_WARNING = 0.20
TEMPLATE_RATE_WARNING = 0.40
DUPLICATE_RATE_WARNING = 0.20
GROUNDED_RATE_WARNING = 0.60

_DRAFT_SQL = """
SELECT d.draft_id, d.media_pk, d.text, d.length, d.quality_ok, d.quality_reason,
       d.similarity_max, d.status, d.generator,
       m.caption, m.hashtags
FROM comment_drafts d
LEFT JOIN candidate_media m ON m.media_pk = d.media_pk
ORDER BY d.draft_id
"""


def _tokens(text: str) -> set[str]:
    return {
        token
        for token in _TOKEN_RE.findall((text or "").lower())
        if token not in _STOPWORDS
    }


def reason_family(reason: str) -> str:
    """'duplicate(0.89)' · 'generic:잘 보고' 처럼 값이 붙은 사유를 종류로 묶는다."""
    text = (reason or "").strip()
    if not text:
        return ""
    for separator in ("(", ":"):
        index = text.find(separator)
        if index > 0:
            return text[:index]
    return text


@dataclass
class CommentSample:
    """댓글 한 건의 측정값."""

    draft_id: int
    media_pk: int
    text: str
    generator: str
    status: str
    quality_ok: bool
    quality_reason: str
    similarity_max: float
    caption: str = ""
    hashtags: tuple[str, ...] = ()

    @property
    def length(self) -> int:
        return len(self.text)

    @property
    def emoji_count(self) -> int:
        return len(_EMOJI_RE.findall(self.text))

    @property
    def is_question(self) -> bool:
        return "?" in self.text

    @property
    def shared_tokens(self) -> tuple[str, ...]:
        """댓글과 caption(+해시태그)이 함께 쓴 낱말.

        한국어는 낱말 뒤에 조사가 붙는다 — caption의 '한강이'와 댓글의 '한강'은
        같은 말이다. 그래서 완전히 같은 글자만 세면 겹친 것을 놓친다.
        한쪽이 다른 쪽으로 시작하면 같은 낱말로 본다.
        """
        source = _tokens(self.caption) | {tag.lower() for tag in self.hashtags}
        shared = set()
        for token in _tokens(self.text):
            for other in source:
                if token == other or token.startswith(other) or other.startswith(token):
                    shared.add(min(token, other, key=len))
                    break
        return tuple(sorted(shared))

    @property
    def grounded(self) -> bool:
        """그 게시물의 내용을 하나라도 집어 말했는지.

        '좋은 영상이네요'처럼 아무 게시물에나 붙는 댓글과, 그 게시물을 본
        사람만 쓸 수 있는 댓글을 가르는 기준이다.
        """
        return bool(self.shared_tokens)

    def as_row(self) -> dict[str, str]:
        return {
            "draft_id": str(self.draft_id),
            "생성기": self.generator,
            "상태": self.status,
            "게이트": "통과" if self.quality_ok else f"거절({reason_family(self.quality_reason)})",
            "길이": str(self.length),
            "이모지": str(self.emoji_count),
            "caption 근거": ",".join(self.shared_tokens) or "없음",
            "중복유사도": f"{self.similarity_max:.2f}",
        }


@dataclass
class CommentCalibration:
    """Phase 18D.3 결과. Prompt를 바꿀 근거가 있는지만 판정한다."""

    samples: list[CommentSample] = field(default_factory=list)
    findings: list[str] = field(default_factory=list)
    recommendations: list[str] = field(default_factory=list)
    prompt_change_warranted: bool = False

    @property
    def total(self) -> int:
        return len(self.samples)

    @property
    def passed(self) -> list[CommentSample]:
        return [item for item in self.samples if item.quality_ok]

    @property
    def rejected(self) -> list[CommentSample]:
        return [item for item in self.samples if not item.quality_ok]

    def _rate(self, count: int, total: Optional[int] = None) -> Optional[float]:
        denominator = self.total if total is None else total
        return (count / denominator) if denominator else None

    @property
    def reject_rate(self) -> Optional[float]:
        return self._rate(len(self.rejected))

    @property
    def template_rate(self) -> Optional[float]:
        return self._rate(sum(1 for item in self.samples if item.generator == "template"))

    @property
    def reject_reasons(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for item in self.rejected:
            family = reason_family(item.quality_reason) or "(사유없음)"
            counts[family] = counts.get(family, 0) + 1
        return counts

    @property
    def grounded_rate(self) -> Optional[float]:
        """**통과한** 댓글 중 caption에 근거한 비율.

        거절된 댓글까지 섞으면 게이트 성능과 내용 품질이 뒤섞인다.
        """
        passed = self.passed
        return self._rate(sum(1 for item in passed if item.grounded), len(passed))

    @property
    def duplicate_rate(self) -> Optional[float]:
        return self._rate(
            sum(1 for item in self.samples if reason_family(item.quality_reason) == "duplicate")
        )

    @property
    def generic_rate(self) -> Optional[float]:
        return self._rate(
            sum(
                1
                for item in self.samples
                if reason_family(item.quality_reason) in ("generic", "banned_phrase")
            )
        )

    @property
    def average_length(self) -> Optional[float]:
        passed = self.passed
        return (sum(item.length for item in passed) / len(passed)) if passed else None

    def as_rows(self) -> dict[str, str]:
        from .snapshot import pct

        return {
            "댓글 표본": str(self.total),
            "게이트 통과 / 거절": (
                f"{len(self.passed)} / {len(self.rejected)} ({pct(self.reject_rate)})"
            ),
            "거절 사유": ", ".join(f"{k}={v}" for k, v in sorted(self.reject_reasons.items()))
            or "없음",
            "generic 계열 비율": pct(self.generic_rate),
            "중복 거절 비율": pct(self.duplicate_rate),
            "Template 비율": pct(self.template_rate),
            "caption 근거 비율(통과분)": pct(self.grounded_rate),
            "평균 길이(통과분)": (
                f"{self.average_length:.1f}자" if self.average_length is not None else "-"
            ),
            "Prompt 수정 근거": "있음" if self.prompt_change_warranted else "없음",
        }


def collect_comment_calibration(
    conn: sqlite3.Connection, config: Config
) -> CommentCalibration:
    """저장된 댓글만 읽어 품질을 센다. **Claude를 부르지 않는다.**"""
    result = CommentCalibration()
    cursor = conn.execute(_DRAFT_SQL)
    columns = [description[0] for description in cursor.description]
    for values in cursor.fetchall():
        row = dict(zip(columns, values))
        try:
            hashtags = tuple(str(tag).lower() for tag in json.loads(row["hashtags"] or "[]"))
        except (TypeError, ValueError):
            hashtags = ()
        result.samples.append(
            CommentSample(
                draft_id=int(row["draft_id"]),
                media_pk=int(row["media_pk"] or 0),
                text=str(row["text"] or ""),
                generator=str(row["generator"] or ""),
                status=str(row["status"] or ""),
                quality_ok=bool(row["quality_ok"]),
                quality_reason=str(row["quality_reason"] or ""),
                similarity_max=float(row["similarity_max"] or 0.0),
                caption=str(row["caption"] or ""),
                hashtags=hashtags,
            )
        )

    _add_findings(result, config)
    return result


def _add_findings(result: CommentCalibration, config: Config) -> None:
    """Prompt를 바꿀 **객관적 근거**가 있는지만 따진다(§25).

    '더 자연스럽게' 같은 이유로는 바꾸지 않는다. 표본이 적으면 판단을 미룬다.
    """
    from .snapshot import pct

    if result.total < MIN_SAMPLES_FOR_WARNING:
        result.findings.append(
            f"댓글 표본이 {result.total}건뿐입니다(최소 {MIN_SAMPLES_FOR_WARNING}건) — "
            "비율로 판단하지 않습니다."
        )
        result.recommendations.append("Prompt 유지 — 표본이 쌓인 뒤 다시 봅니다.")
        return

    triggers: list[str] = []
    checks = (
        ("게이트 거절", result.reject_rate, REJECT_RATE_WARNING, True),
        ("generic 계열 거절", result.generic_rate, GENERIC_RATE_WARNING, True),
        ("Template 대체", result.template_rate, TEMPLATE_RATE_WARNING, True),
        ("중복 거절", result.duplicate_rate, DUPLICATE_RATE_WARNING, True),
        ("caption 근거", result.grounded_rate, GROUNDED_RATE_WARNING, False),
    )
    for label, value, limit, higher_is_worse in checks:
        if value is None:
            continue
        exceeded = value >= limit if higher_is_worse else value < limit
        if exceeded:
            direction = "높습니다" if higher_is_worse else "낮습니다"
            triggers.append(f"{label} 비율이 {pct(value)}로 {direction}(기준 {pct(limit)})")

    if triggers:
        result.prompt_change_warranted = True
        result.findings.extend(triggers)
        result.recommendations.append(
            "Prompt 조정을 검토하세요 — 바꾼 뒤에는 같은 표본으로 Old/New를 비교해야 합니다."
        )
    else:
        result.findings.append(
            f"거절 {pct(result.reject_rate)} · generic {pct(result.generic_rate)} · "
            f"Template {pct(result.template_rate)} · caption 근거 {pct(result.grounded_rate)} — "
            "모두 기준 안입니다."
        )
        result.recommendations.append(
            "Prompt 유지 — 바꿀 객관적 근거가 없습니다(추상적인 이유로 바꾸지 않습니다)."
        )

    ungrounded = [item for item in result.passed if not item.grounded]
    if ungrounded:
        result.findings.append(
            f"게이트를 통과했지만 caption 낱말을 하나도 집지 않은 댓글이 {len(ungrounded)}건입니다 "
            "— 어느 게시물에나 붙을 수 있는 문장인지 확인하세요."
        )


# --- Prompt A/B 비교 --------------------------------------------------------
@dataclass
class VariantScore:
    """댓글 묶음 하나의 품질 요약(Prompt 비교용)."""

    label: str
    total: int = 0
    passed: int = 0
    grounded: int = 0
    reject_reasons: dict[str, int] = field(default_factory=dict)
    average_length: float = 0.0

    @property
    def pass_rate(self) -> Optional[float]:
        return (self.passed / self.total) if self.total else None

    @property
    def grounded_rate(self) -> Optional[float]:
        return (self.grounded / self.passed) if self.passed else None

    def as_row(self) -> dict[str, str]:
        from .snapshot import pct

        return {
            "표본": str(self.total),
            "게이트 통과": f"{self.passed} ({pct(self.pass_rate)})",
            "caption 근거(통과분)": pct(self.grounded_rate),
            "평균 길이": f"{self.average_length:.1f}자",
            "거절 사유": ", ".join(f"{k}={v}" for k, v in sorted(self.reject_reasons.items()))
            or "없음",
        }


def score_variant(
    label: str,
    comments: "list[tuple[str, str, tuple[str, ...]]]",
    config: Config,
) -> VariantScore:
    """(댓글, caption, 해시태그) 묶음을 **운영과 같은 품질 게이트**로 채점한다.

    Prompt를 바꿨을 때 Old/New를 같은 표본·같은 기준으로 비교하기 위한 것이다
    (§26). Claude를 부르지 않고, 운영 DB에 아무것도 쓰지 않는다.
    """
    from ..comments.quality_filter import CommentQualityFilter

    quality = CommentQualityFilter(config)
    score = VariantScore(label=label, total=len(comments))
    lengths: list[int] = []
    for text, caption, hashtags in comments:
        sample = CommentSample(
            draft_id=0,
            media_pk=0,
            text=text,
            generator=label,
            status="",
            quality_ok=False,
            quality_reason="",
            similarity_max=0.0,
            caption=caption,
            hashtags=hashtags,
        )
        context = list(hashtags) + sorted(_tokens(caption))
        checked = quality.check(text, context)
        sample.quality_ok = checked.ok
        sample.quality_reason = checked.reason
        if checked.ok:
            score.passed += 1
            lengths.append(sample.length)
            if sample.grounded:
                score.grounded += 1
        else:
            family = reason_family(checked.reason) or "(사유없음)"
            score.reject_reasons[family] = score.reject_reasons.get(family, 0) + 1
    score.average_length = (sum(lengths) / len(lengths)) if lengths else 0.0
    return score
