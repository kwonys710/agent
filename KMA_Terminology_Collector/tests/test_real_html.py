"""term.kma.org 실제 HTML(2026-11-05 브라우저 열람 저장본) 회귀 테스트.

samples/a_substring/p1~3.html : 6.1판, 검색어 'a'(부분 일치), 1~3페이지
tests/fixtures/prefix_p2.html : 6.1판, A–Z 색인 'a'(spelling_chk=1) 2페이지 앞 3행
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

from openpyxl import load_workbook

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
import kma_collect as kc  # noqa: E402
from exporter import export  # noqa: E402
from parser import parse_list, split_variants  # noqa: E402
from validator import validate  # noqa: E402

SAMPLES = ROOT / "samples" / "a_substring"
FIXTURE = ROOT / "tests" / "fixtures" / "prefix_p2.html"
# 실제 KMA 데이터는 저작권상 공개 저장소에 올리지 않는다(.gitignore). 없으면 건너뜀.
EXPECTED = ROOT / "tests" / "fixtures" / "real_expected.json"  # 기대값(실제 용어)도 로컬 전용
HAVE_DATA = (SAMPLES / "p3.html").is_file() and FIXTURE.is_file() and EXPECTED.is_file()
PREFIX_P2 = FIXTURE.read_text(encoding="utf-8") if HAVE_DATA else ""
E = json.loads(EXPECTED.read_text(encoding="utf-8")) if HAVE_DATA else {}


@unittest.skipUnless(HAVE_DATA, "실제 KMA 샘플 HTML 없음(로컬 전용 데이터)")
class RealHtmlTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = kc.load_config()
        for k in ("raw_dir", "output_dir", "report_dir", "log_dir"):
            self.cfg["paths"][k] = str(Path(self.tmp) / k)
        self.conn = kc.open_state(Path(self.tmp) / "s.sqlite")
        kc.set_meta(self.conn, "mode", "import")

    def rows(self, page):
        return parse_list((SAMPLES / f"p{page}.html").read_text(encoding="utf-8"), self.cfg, "u")

    def test_structure_total_and_exact_text(self):
        rows, total = self.rows(1)
        self.assertEqual((len(rows), total), (15, E["page1_total"]))
        by = {r["english_term"]: r for r in rows}
        for en, ko in E["highlight_stripped"]:  # 강조 태그 제거
            self.assertEqual(by[en]["korean_term"], ko)
        en, ko = E["comma_kept"]
        self.assertEqual(by[en]["korean_term"], ko)  # 쉼표 원문 유지
        self.assertEqual(parse_list(PREFIX_P2, self.cfg, "u")[1], E["prefix_total"])

    def test_markers_and_verbatim_spacing(self):
        by = {r["english_term"]: r for p in (1, 2, 3) for r in self.rows(p)[0]}
        en, ko, note = E["original_marker"]
        self.assertEqual((by[en]["korean_term"], by[en]["notes"]), (ko, note))
        for key, field in (("abbreviation", "abbreviation"), ("korean_synonym", "korean_alternative"),
                           ("english_synonym", "english_synonym")):
            en, val = E[key]
            self.assertEqual(by[en][field], val)
        en, ko = E["double_space"]
        self.assertEqual(by[en]["korean_term"], ko)  # 원문 이중 공백 보존
        self.assertTrue(all(r["term_id"].startswith("h_") and r["term_id_source"] == "hash"
                            for r in by.values()))

    def test_variant_split_keeps_original(self):
        sep = self.cfg["parser"]["variant_separator"]
        by = {r["english_term"]: r for r in self.rows(2)[0]}
        en, parts, original = E["variant_split"]
        v = split_variants(by[en], sep)
        self.assertEqual([x["korean_variant"] for x in v], parts)
        self.assertEqual(v[0]["original_korean"], original)
        self.assertEqual(split_variants(by[E["no_split"]], sep), [])

    def test_import_three_pages_end_to_end(self):
        kc.collect(self.cfg, self.conn, kc.make_file_fetcher(self.cfg, SAMPLES), max_pages=3,
                   sleep=lambda _: None)
        ok = [r[0] for r in self.conn.execute("SELECT page_number FROM pages WHERE status='ok'")]
        self.assertEqual(ok, [1, 2, 3])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM terms").fetchone()[0], 45)
        url = self.conn.execute("SELECT url FROM pages WHERE page_number=2").fetchone()[0]
        self.assertIn("pageno=2", url)
        self.assertIn("sch_type1=twdContent7", url)
        checks = {c["check"]: c["status"] for c in validate(self.conn)}
        self.assertEqual(checks["실패 페이지"], "PASS")
        self.assertEqual(checks["판본 일관성"], "PASS")
        self.assertEqual(checks["특수문자/인코딩 깨짐"], "PASS")

        out = export(self.conn, self.cfg, Path(self.tmp))
        self.assertIn("REAL_SAMPLE_import", out["csv"])
        self.assertEqual(Path(out["csv"]).read_bytes()[:3], b"\xef\xbb\xbf")
        self.assertIn(E["in_csv"], Path(out["csv"]).read_text(encoding="utf-8-sig"))
        wb = load_workbook(out["xlsx"])
        self.assertEqual(len(wb.sheetnames), 6)
        self.assertGreater(wb["복수대응어"].max_row, 1)

    def test_search_condition_change_stops(self):
        """사이트 페이지 링크처럼 검색 조건이 바뀌면(총건수 변경) 기록 후 중단."""
        pages = {1: (SAMPLES / "p1.html").read_text(encoding="utf-8"), 2: PREFIX_P2}
        kc.collect(self.cfg, self.conn, lambda p: (pages[p], f"u{p}"), max_pages=2, sleep=lambda _: None)
        st, err = self.conn.execute("SELECT status, error FROM pages WHERE page_number=2").fetchone()
        self.assertEqual(st, "failed")
        self.assertIn("38793→4233", err)

    def test_missing_saved_page_is_failure_not_end(self):
        kc.collect(self.cfg, self.conn, kc.make_file_fetcher(self.cfg, SAMPLES), max_pages=4,
                   sleep=lambda _: None)
        self.assertEqual(self.conn.execute("SELECT status FROM pages WHERE page_number=4").fetchone()[0],
                         "failed")
        self.assertIsNone(kc.get_meta(self.conn, "last_page_reached"))

    def test_live_modes_blocked_by_real_robots(self):
        import urllib.robotparser
        rp = urllib.robotparser.RobotFileParser()
        rp.parse("User-Agent : *\nDisallow : /\n".splitlines())  # 2026-11-05 관찰 원문
        self.assertFalse(rp.can_fetch(self.cfg["site"]["user_agent"],
                                      self.cfg["site"]["base_url"] + self.cfg["site"]["list_path"]))


if __name__ == "__main__":
    unittest.main()
