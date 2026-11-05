"""상태 DB → CSV(UTF-8 BOM) + XLSX(6개 시트) + 검증 보고서."""
import csv
import json
from datetime import datetime

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter

from parser import FIELDS
from validator import validate

BASE_COLS = ["term_id", "english_term", "korean_term", *FIELDS[2:], "source_edition", "source_url",
             "detail_url", "page_number", "collected_at", "term_id_source"]


def _rows(conn, sql):
    cur = conn.execute(sql)
    return [d[0] for d in cur.description], [list(r) for r in cur.fetchall()]


def _sheet(wb, title, header, rows):
    ws = wb.create_sheet(title)
    ws.append(header)
    for r in rows:
        ws.append(["" if v is None else (json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v)
                   for v in r])
    for c in ws[1]:
        c.font, c.fill = Font(bold=True, color="FFFFFF"), PatternFill("solid", fgColor="305496")
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    for i, h in enumerate(header, 1):
        width = max([len(str(h))] + [len(str(r[i - 1])) for r in rows[:500] if r[i - 1] is not None])
        ws.column_dimensions[get_column_letter(i)].width = min(max(width + 2, 10), 60)
    return ws


def export(conn, cfg, root, source="state.sqlite"):
    out_dir, rep_dir = root / cfg["paths"]["output_dir"], root / cfg["paths"]["report_dir"]
    out_dir.mkdir(parents=True, exist_ok=True)
    rep_dir.mkdir(parents=True, exist_ok=True)

    # 원본에 데이터가 하나도 없는 선택 컬럼은 출력하지 않는다(값을 만들어내지 않음).
    # 단 대체 용어(korean_alternative)는 필수 컬럼이라 비어 있어도 공란으로 둔다.
    present = [c for c in BASE_COLS if c not in FIELDS[2:] or c == "korean_alternative" or
               conn.execute(f"SELECT 1 FROM terms WHERE TRIM(COALESCE({c},''))<>'' LIMIT 1").fetchone()]
    header, terms = _rows(conn, f"SELECT {', '.join(present)} FROM terms "
                                f"ORDER BY english_term COLLATE NOCASE, korean_term")

    mode = json.loads((conn.execute("SELECT value FROM meta WHERE key='mode'").fetchone() or ['""'])[0])
    stem = f"KMA_의학용어집_{cfg['parser']['edition_label']}"
    if mode in ("sample", "import"):
        stem += f"_REAL_SAMPLE_{mode}"  # 전체 데이터가 아님을 파일명에 표시
    csv_path = out_dir / f"{stem}.csv"
    with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f, quoting=csv.QUOTE_MINIMAL)
        w.writerow(header)
        w.writerows(terms)

    checks = validate(conn, cfg["pagination"]["start_page"])
    meta = {k: json.loads(v) for k, v in conn.execute("SELECT key, value FROM meta")}
    exported_at = datetime.now().astimezone().isoformat(timespec="seconds")

    wb = Workbook()
    wb.remove(wb.active)
    _sheet(wb, "의학용어_전체", header, terms)
    _sheet(wb, "복수대응어", *_rows(conn, "SELECT * FROM term_variants ORDER BY english_term, variant_order"))
    _sheet(wb, "중복검증", *_rows(conn, "SELECT * FROM term_dupes ORDER BY term_id, dup_page"))
    _sheet(wb, "수집페이지_이력", *_rows(conn, "SELECT * FROM pages ORDER BY page_number"))
    err_h = ["구분", "대상", "내용"]
    errs = [["실패 페이지", r[0], r[1]] for r in
            conn.execute("SELECT page_number, error FROM pages WHERE status IN ('failed','repeat')")]
    errs += [["영문 누락", r[0], r[1]] for r in
             conn.execute("SELECT term_id, korean_term FROM terms WHERE TRIM(COALESCE(english_term,''))=''")]
    errs += [["한글 누락", r[0], r[1]] for r in
             conn.execute("SELECT term_id, english_term FROM terms WHERE TRIM(COALESCE(korean_term,''))=''")]
    errs += [["검증 " + c["status"], c["check"], c["detail"]] for c in checks if c["status"] != "PASS"]
    _sheet(wb, "오류및누락", err_h, errs)
    meta_rows = [["출처", meta.get("source_list_url")], ["판본 표시", cfg["parser"]["edition_label"]],
                 ["내보낸 시각", exported_at], ["상태 DB", source], ["수집 모드", meta.get("mode")],
                 ["승인 정보", meta.get("permission")], ["사이트 전체 건수", meta.get("site_total_count")],
                 ["마지막 페이지", meta.get("last_page_reached")], ["종료 사유", meta.get("stop_reason")],
                 ["설정 스냅샷", meta.get("config_snapshot")]]
    meta_rows += [[f"검증: {c['check']}", f"{c['status']} — {c['detail']}"] for c in checks]
    _sheet(wb, "수집메타데이터", ["항목", "값"], meta_rows)
    xlsx_path = out_dir / f"{stem}.xlsx"
    wb.save(xlsx_path)

    report = {"exported_at": exported_at, "state_db": source, "rows": len(terms), "checks": checks}
    (rep_dir / "validation_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1),
                                                    encoding="utf-8")
    return {"csv": str(csv_path), "xlsx": str(xlsx_path), "rows": len(terms),
            "checks": {c["check"]: c["status"] for c in checks}}
