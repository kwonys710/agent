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


# term.kma.org <dd> 표지(2026-11-05 관찰): [원] 원어, [동] 동의어(한글/영문), [약] 약어.
MARKER_RE = re.compile(r"\s*\[([^\]\s]{1,3})\]\s*")
HANGUL = re.compile(r"[가-힣]")


def _verbatim(tag):
    # 강조 태그(<font><b>)만 제거. "1. 유산  2. 낙태" 처럼 원문 내부 공백은 그대로 둔다.
    return re.sub(r"[\t\r\n]", " ", tag.get_text()).strip()


def _parse_dd(dd_raw):
    parts = MARKER_RE.split(dd_raw)
    row = {"korean_term": parts[0].strip()}
    notes = []
    for key, val in zip(parts[1::2], parts[2::2]):
        val = val.strip()
        if key == "동":
            field = "korean_alternative" if HANGUL.search(val) else "english_synonym"
        elif key == "약":
            field = "abbreviation"
        else:  # [원] 및 미확인 표지는 원문 표기 그대로 notes 에 보존
            notes.append(f"[{key}] {val}")
            continue
        row[field] = f"{row[field]} | {val}" if row.get(field) else val
    if notes:
        row["notes"] = " ; ".join(notes)
    return row


def _parse_dl(soup, p):
    dl = soup.select_one(p["row_selector"])
    rows = []
    for dt in dl.find_all("dt") if dl else []:
        dd = dt.find_next_sibling()
        dd_raw = _verbatim(dd) if dd is not None and dd.name == "dd" else ""
        row = {"english_term": _verbatim(dt), **_parse_dd(dd_raw), "detail_url": ""}
        row["term_id"] = "h_" + term_key(row["english_term"], dd_raw)  # 사이트 ID 없음
        row["term_id_source"] = "hash"
        rows.append(row)
    return rows


def parse_list(html, cfg, page_url):
    """returns (rows, total_count or None). rows 는 원본에 있는 컬럼만 채운다."""
    p = cfg["parser"]
    if not p.get("row_selector"):
        raise ValueError("parser.row_selector 미설정 — inspect 결과로 먼저 확정할 것")
    soup = BeautifulSoup(html, "html.parser")
    if p.get("layout") == "dl":
        return _parse_dl(soup, p), _total(soup, p)
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
    return rows, _total(soup, p)


def _total(soup, p):
    if not p.get("total_count_regex"):
        return None
    m = re.search(p["total_count_regex"], clean(soup.get_text(" ")))
    return int(m.group(1).replace(",", "")) if m else None


def split_variants(row, separator):
    """원본 구분자(정규식)가 설정된 경우에만 복수 대응어를 분리. 원 문자열은 korean_term 에 그대로 둔다."""
    if not separator:
        return []
    parts = [x for x in (clean(s) for s in re.split(separator, row.get("korean_term", ""))) if x]
    if len(parts) < 2:
        return []
    return [{"term_id": row["term_id"], "english_term": row.get("english_term", ""),
             "variant_order": i + 1, "korean_variant": v, "original_korean": row["korean_term"]}
            for i, v in enumerate(parts)]
