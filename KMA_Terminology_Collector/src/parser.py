"""목록 HTML → 용어 행. 사이트 구조는 settings.yaml(inspect 결과로 확정)에서만 가져온다."""
import hashlib
import re
from urllib.parse import parse_qs, urljoin, urlparse

from bs4 import BeautifulSoup

FIELDS = ["english_term", "korean_term", "korean_alternative", "english_synonym",
          "abbreviation", "category", "notes"]


def clean(text):
    # 태그 제거 후 공백·줄바꿈만 정리. 용어 표기(대소문자, 괄호, 구두점)는 건드리지 않는다.
    return re.sub(r"\s+", " ", text or "").strip()


def _column_index(headers, header_map):
    idx = {}
    for field, pattern in header_map.items():
        if not pattern:
            continue
        hits = [i for i, h in enumerate(headers) if re.search(pattern, h)]
        if len(hits) != 1:
            raise ValueError(f"header_map[{field}]={pattern!r} 가 헤더 {headers} 중 {len(hits)}개와 일치")
        idx[field] = hits[0]
    if "english_term" not in idx or "korean_term" not in idx:
        raise ValueError("header_map 에 english_term, korean_term 은 필수")
    return idx


def term_key(english, korean):
    return hashlib.sha1(f"{english}\x1f{korean}".encode("utf-8")).hexdigest()[:16]


def parse_list(html, cfg, page_url):
    """returns (rows, total_count or None). rows 는 원본에 있는 컬럼만 채운다."""
    p = cfg["parser"]
    if not p.get("row_selector"):
        raise ValueError("parser.row_selector 미설정 — inspect 결과로 먼저 확정할 것")
    soup = BeautifulSoup(html, "html.parser")
    trs = soup.select(p["row_selector"])

    idx = {}
    if trs:
        table = trs[0].find_parent("table")
        head = (table.select("thead th") or table.select("tr th")) if table else []
        idx = _column_index([clean(h.get_text(" ")) for h in head], p["header_map"])

    rows = []
    for tr in trs:
        tds = tr.find_all("td")
        if not tds:
            continue  # 헤더 행
        row = {f: clean(tds[i].get_text(" ")) for f, i in idx.items() if i < len(tds)}
        link = tr.select_one(p.get("detail_link_selector") or "a[href]")
        href = link.get("href", "") if link else ""
        detail = urljoin(page_url, href) if href and not href.lower().startswith("javascript") else ""
        row["detail_url"] = detail
        qid = p.get("term_id_query_param")
        site_id = parse_qs(urlparse(detail).query).get(qid, [""])[0] if (qid and detail) else ""
        row["term_id"] = site_id or "h_" + term_key(row.get("english_term", ""), row.get("korean_term", ""))
        row["term_id_source"] = "site" if site_id else "hash"
        rows.append(row)

    total = None
    if p.get("total_count_regex"):
        m = re.search(p["total_count_regex"], clean(soup.get_text(" ")))
        total = int(m.group(1).replace(",", "")) if m else None
    return rows, total


def split_variants(row, separator):
    """원본 구분자가 설정된 경우에만 복수 대응어를 분리. 원 문자열은 korean_term 에 그대로 둔다."""
    if not separator:
        return []
    parts = [x for x in (clean(s) for s in row.get("korean_term", "").split(separator)) if x]
    if len(parts) < 2:
        return []
    return [{"term_id": row["term_id"], "english_term": row.get("english_term", ""),
             "variant_order": i + 1, "korean_variant": v, "original_korean": row["korean_term"]}
            for i, v in enumerate(parts)]
