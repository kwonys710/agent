"""수집 파이프라인 자체 검증.

주의: 아래 HTML 은 **합성(SYNTHETIC) 테스트 데이터**다. term.kma.org 의 실제 구조가 아니며,
사이트 접속이 막힌 상태에서 페이지 순회·중복 제거·재개·출력 로직만 검증한다.
실제 사이트 파서 검증은 --mode inspect 성공 후 --mode sample 로 별도 수행해야 한다.
"""
import csv
import json
import sys
import tempfile
import unittest
from pathlib import Path

from openpyxl import load_workbook

SRC = Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))
import kma_collect as kc  # noqa: E402
from exporter import export  # noqa: E402
from parser import parse_list, split_variants  # noqa: E402

PAGES = {  # SYNTHETIC
    1: [("abdomen", "배"), ("abscess", "고름집"), ("acne", "여드름")],
    2: [("acne", "여드름"), ("alveolus", "허파꽈리, 폐포"), ("aneurysm", "동맥류 (동맥꽈리)")],
    3: [("kidney", "콩팥"), ("Na⁺/K⁺-ATPase", "나트륨칼륨펌프")],
    4: [],
}


def synthetic_html(page):
    rows = "".join(f'<tr><td><a href="view.asp?tid=T{e}">{e}</a></td><td>\n  {k} </td></tr>'
                   for e, k in PAGES.get(page, []))
    return (f"<html><body><p>총 8 건</p><table class='res'><thead><tr><th>영문</th><th>한글</th></tr></thead>"
            f"<tbody>{rows}</tbody></table></body></html>")


def make_cfg(tmp):
    cfg = kc.load_config()
    # 합성 표(table) 구조용 설정. 실제 사이트 값(settings.yaml: layout=dl 등)과 분리한다.
    cfg["pagination"].update(method="GET", page_param="page", page_size=3, fixed_params={})
    cfg["parser"].update(layout="table", row_selector="table.res tbody tr",
                         total_count_regex=r"총\s*([\d,]+)\s*건", term_id_query_param="tid",
                         variant_separator=None, edition_label="미확인")
    cfg["parser"]["header_map"].update(english_term="^영문$", korean_term="^한글$")
    for k in ("raw_dir", "output_dir", "report_dir", "log_dir"):
        cfg["paths"][k] = str(Path(tmp) / k)  # 절대경로 → ROOT 대신 임시폴더 사용
    return cfg


def fake_fetch(fail_on=None, calls=None):
    state = {"failed": False}

    def fetch(page):
        if calls is not None:
            calls.append(page)
        if page == fail_on and not state["failed"]:
            state["failed"] = True
            raise ConnectionError("simulated network error")
        return synthetic_html(page), f"https://example.test/list.asp?page={page}"
    return fetch


def no_sleep(_):
    pass


class PipelineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = make_cfg(self.tmp)
        self.conn = kc.open_state(Path(self.tmp) / "state.sqlite")
        kc.set_meta(self.conn, "mode", "full")

    def count(self, sql):
        return self.conn.execute(sql).fetchone()[0]

    def test_parser_keeps_original_text(self):
        rows, total = parse_list(synthetic_html(2), self.cfg, "https://example.test/list.asp")
        self.assertEqual(total, 8)
        self.assertEqual(rows[1]["korean_term"], "허파꽈리, 폐포")  # 쉼표를 구분자로 쓰지 않음
        self.assertEqual(rows[2]["korean_term"], "동맥류 (동맥꽈리)")
        self.assertEqual(rows[0]["term_id"], "Tacne")
        self.assertEqual(split_variants(rows[1], None), [])
        self.assertEqual([v["korean_variant"] for v in split_variants(rows[1], ",")], ["허파꽈리", "폐포"])

    def test_collect_paginates_dedupes_and_stops(self):
        kc.collect(self.cfg, self.conn, fake_fetch(), sleep=no_sleep)
        self.assertEqual(self.count("SELECT COUNT(*) FROM pages WHERE status='ok'"), 3)
        self.assertEqual(self.count("SELECT COUNT(*) FROM terms"), 7)  # 8행 중 acne 중복 1건 제거
        self.assertEqual(self.count("SELECT COUNT(*) FROM term_dupes"), 1)
        self.assertEqual(kc.get_meta(self.conn, "last_page_reached"), 3)
        self.assertEqual(kc.get_meta(self.conn, "stop_reason"), "total_count_reached")

    def test_empty_page_and_repeat_detection(self):
        self.cfg["pagination"]["page_size"] = None  # 건수 기반 종료 끔 → 빈 목록으로 종료
        kc.collect(self.cfg, self.conn, fake_fetch(), sleep=no_sleep)
        self.assertEqual(kc.get_meta(self.conn, "stop_reason"), "empty_list")
        conn2 = kc.open_state(Path(self.tmp) / "repeat.sqlite")
        kc.collect(self.cfg, conn2, lambda p: (synthetic_html(1), "u"), sleep=no_sleep)
        self.assertEqual(kc.get_meta(conn2, "stop_reason"), "repeated_page")
        self.assertEqual(conn2.execute("SELECT COUNT(*) FROM pages WHERE status='ok'").fetchone()[0], 1)

    def test_resume_continues_after_last_good_page(self):
        calls = []
        fetch = fake_fetch(fail_on=2, calls=calls)
        kc.collect(self.cfg, self.conn, fetch, sleep=no_sleep)
        self.assertEqual(self.count("SELECT status FROM pages WHERE page_number=2"), "failed")
        kc.collect(self.cfg, self.conn, fetch, sleep=no_sleep)  # resume
        self.assertEqual(calls, [1, 2, 2, 3])  # 1페이지는 다시 받지 않음
        self.assertEqual(self.count("SELECT COUNT(*) FROM terms"), 7)

    def test_stop_status_halts_immediately(self):
        calls = []

        def fetch(page):
            calls.append(page)
            raise kc.StopCollect("HTTP 429")
        kc.collect(self.cfg, self.conn, fetch, sleep=no_sleep)
        self.assertEqual(calls, [1])
        self.assertEqual(self.count("SELECT error FROM pages"), "HTTP 429")

    def test_export_csv_xlsx(self):
        kc.collect(self.cfg, self.conn, fake_fetch(), sleep=no_sleep)
        out = export(self.conn, self.cfg, Path(self.tmp))
        raw = Path(out["csv"]).read_bytes()
        self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))
        rows = list(csv.reader(raw.decode("utf-8-sig").splitlines()))
        self.assertEqual(rows[0][:3], ["term_id", "english_term", "korean_term"])
        self.assertNotIn("abbreviation", rows[0])  # 원본에 없는 컬럼은 만들지 않음
        self.assertIn("korean_alternative", rows[0])  # 필수 컬럼(대체 용어)은 비어도 공란 유지
        self.assertIn(["Talveolus", "alveolus", "허파꽈리, 폐포"], [r[:3] for r in rows])
        self.assertIn("Na⁺/K⁺-ATPase", [r[1] for r in rows])
        self.assertEqual([r[1] for r in rows[1:]], sorted((r[1] for r in rows[1:]), key=str.lower))
        wb = load_workbook(out["xlsx"])
        self.assertEqual(wb.sheetnames, ["의학용어_전체", "복수대응어", "중복검증", "수집페이지_이력",
                                         "오류및누락", "수집메타데이터"])
        self.assertEqual(wb["의학용어_전체"].freeze_panes, "A2")
        self.assertEqual(out["checks"]["중복 제거 전후"], "PASS")
        self.assertEqual(out["checks"]["판본 일관성"], "WARN")  # edition_label=미확인

    def test_permission_gate(self):
        with self.assertRaises(SystemExit):
            kc.load_permission(None)
        p = Path(self.tmp) / "permission.json"
        p.write_text(json.dumps({"approving_org": "대한의사협회 의학용어위원회"}), encoding="utf-8")
        with self.assertRaises(SystemExit):
            kc.load_permission(p)
        good = {"approving_org": "x", "approval_date": "2026-01-01", "scope": "s", "purpose": "p",
                "approval_reference": "r", "bulk_collection_allowed": True}
        p.write_text(json.dumps(good), encoding="utf-8")
        self.assertEqual(kc.load_permission(p)["approving_org"], "x")
        p.write_text(json.dumps(dict(good, bulk_collection_allowed=False)), encoding="utf-8")
        with self.assertRaises(SystemExit):
            kc.load_permission(p)

    def test_unconfigured_site_refuses_to_run(self):
        cfg = kc.load_config()
        cfg["pagination"]["method"] = None  # 관찰 전 상태(null)
        with self.assertRaises(SystemExit):
            kc.require_site_config(cfg)
        kc.require_site_config(kc.load_config())  # 실제 관찰값으로 채운 현재 설정은 통과


if __name__ == "__main__":
    unittest.main(verbosity=2)
