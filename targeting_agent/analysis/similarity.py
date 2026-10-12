"""유사도 계산(외부 Embedding API 없이 동작하는 경량 구현).

두 가지 용도:
1) 콘텐츠 ↔ DailyReels Profile 유사도 (Target Score의 content_similarity)
2) 댓글 ↔ 기존 댓글 유사도 (중복 댓글 차단)

Embedding 모델을 붙이고 싶으면 이 모듈의 함수만 교체하면 된다.
"""
from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter
from difflib import SequenceMatcher
from typing import Iterable, Sequence

TOKEN_RE = re.compile(r"[0-9A-Za-z가-힣]+")
HANGUL_RE = re.compile(r"[가-힣]")

# 의미가 거의 없는 토큰(유사도 계산에서 제외)
STOPWORDS = {
    "그리고", "그래서", "오늘", "진짜", "너무", "정말", "이거", "저거", "합니다",
    "the", "and", "for", "with", "this", "that", "reels", "reel", "instagram",
}


def normalize_text(text: str) -> str:
    """유니코드 정규화 + 공백 정리(한글 자모 분리 입력 대비)."""
    return unicodedata.normalize("NFKC", text or "").strip()


def tokenize(text: str, *, min_length: int = 2) -> list[str]:
    """한글/영문/숫자 토큰으로 분리한다."""
    normalized = normalize_text(text).lower()
    tokens = [t for t in TOKEN_RE.findall(normalized) if len(t) >= min_length]
    return [t for t in tokens if t not in STOPWORDS]


def char_ngrams(text: str, n: int = 2) -> list[str]:
    """한국어 형태소 분석기 없이 부분 일치를 잡기 위한 문자 n-gram."""
    compact = re.sub(r"\s+", "", normalize_text(text).lower())
    if len(compact) < n:
        return [compact] if compact else []
    return [compact[i : i + n] for i in range(len(compact) - n + 1)]


def hangul_ratio(text: str) -> float:
    """문자 중 한글 비율(언어 판정용)."""
    stripped = re.sub(r"\s|#|@", "", text or "")
    if not stripped:
        return 0.0
    return len(HANGUL_RE.findall(stripped)) / len(stripped)


def detect_language(text: str) -> str:
    """간이 언어 판정: 한글 비율 기준."""
    ratio = hangul_ratio(text)
    if ratio >= 0.2:
        return "ko"
    if ratio > 0:
        return "mixed"
    if TOKEN_RE.search(text or ""):
        return "en"
    return "unknown"


def cosine(a: Sequence[str], b: Sequence[str]) -> float:
    """토큰 빈도 기반 코사인 유사도."""
    if not a or not b:
        return 0.0
    ca, cb = Counter(a), Counter(b)
    shared = set(ca) & set(cb)
    if not shared:
        return 0.0
    numerator = sum(ca[t] * cb[t] for t in shared)
    norm_a = math.sqrt(sum(v * v for v in ca.values()))
    norm_b = math.sqrt(sum(v * v for v in cb.values()))
    return numerator / (norm_a * norm_b) if norm_a and norm_b else 0.0


def jaccard(a: Iterable[str], b: Iterable[str]) -> float:
    set_a, set_b = set(a), set(b)
    if not set_a or not set_b:
        return 0.0
    return len(set_a & set_b) / len(set_a | set_b)


def text_similarity(left: str, right: str) -> float:
    """두 텍스트의 의미 근접도(0~1). 댓글 중복 판정에 사용."""
    left_n, right_n = normalize_text(left).lower(), normalize_text(right).lower()
    if not left_n or not right_n:
        return 0.0
    if left_n == right_n:
        return 1.0
    ratio = SequenceMatcher(None, left_n, right_n).ratio()
    token_sim = cosine(tokenize(left_n, min_length=1), tokenize(right_n, min_length=1))
    ngram_sim = jaccard(char_ngrams(left_n), char_ngrams(right_n))
    return max(ratio, token_sim, ngram_sim)


def max_similarity(text: str, corpus: Iterable[str]) -> tuple[float, str]:
    """corpus 중 가장 비슷한 항목과 그 유사도를 반환한다."""
    best_score = 0.0
    best_text = ""
    for other in corpus:
        score = text_similarity(text, other)
        if score > best_score:
            best_score, best_text = score, other
            if best_score >= 0.999:
                break
    return best_score, best_text


def profile_similarity(
    keywords: Sequence[str],
    topics: Sequence[str],
    caption: str,
    profile_keywords: Sequence[str],
    profile_topics: Sequence[str] = (),
) -> float:
    """콘텐츠와 DailyReels Profile의 유사도(0~1).

    키워드/해시태그 겹침(부분 문자열 포함)과 캡션 토큰 겹침을 함께 본다.
    """
    if not profile_keywords:
        return 0.0

    content_tokens = {t.lower() for t in list(keywords) + list(topics)}
    content_tokens |= set(tokenize(caption))
    if not content_tokens:
        return 0.0

    profile_tokens = {k.lower() for k in profile_keywords}
    hits = 0
    for profile_token in profile_tokens:
        if any(profile_token in token or token in profile_token for token in content_tokens):
            hits += 1

    # 프로필 키워드 4개 이상 매칭되면 coverage 1.0 (프로필 키워드 수가 많아도 과소평가 방지)
    coverage = min(hits / 4.0, 1.0)
    topic_hit = jaccard(
        [t.lower() for t in topics], [t.lower() for t in profile_topics]
    ) if profile_topics else 0.0
    overlap = jaccard(content_tokens, profile_tokens)
    return round(min(1.0, 0.6 * coverage + 0.25 * topic_hit + 0.15 * min(overlap * 3, 1.0)), 4)
