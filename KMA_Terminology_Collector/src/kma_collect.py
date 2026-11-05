"""KMA 의학용어 검색목록 수집기 CLI.

  python src/kma_collect.py --mode inspect
  python src/kma_collect.py --mode sample --max-pages 3
  python src/kma_collect.py --mode full   --authorization-file permission.json
  python src/kma_collect.py --mode resume --authorization-file permission.json
  python src/kma_collect.py --mode export
"""
import argparse
import datetime as dt
import hashlib
import json
import logging
import random
import sqlite3
import sys
import time
import urllib.robotparser
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from parser import FIELDS, parse_list, split_variants  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
SAMPLE_MAX_PAGES = 3
log = logging.getLogger("kma")


class StopCollect(Exception):
    """403/429 등 서버가 거부 신호를 보낸 경우 — 재시도하지 않고 즉시 중단."""


def now():
    return dt.datetime.now(dt.timezone.utc).astimezone().isoformat(timespec="seconds")


def load_config(path=ROOT / "config" / "settings.yaml"):
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))


def require_site_config(cfg):
    missing = [k for k, v in [("pagination.method", cfg["pagination"]["method"]),
                              ("pagination.page_param", cfg["pagination"]["page_param"]),
                              ("parser.row_selector", cfg["parser"]["row_selector"]),
                              ("parser.header_map.english_term", cfg["parser"]["header_map"]["english_term"]),
                              ("parser.header_map.korean_term", cfg["parser"]["header_map"]["korean_term"])] if not v]
    if missing:
        raise SystemExit("settings.yaml 미확정 항목: " + ", ".join(missing)
                         + "\n→ --mode inspect 결과(reports/inspect_report.json)로 실제 값을 확인해 채우세요.")


def load_permission(path):
    """full/resume 실행 조건. 필수 항목이 비었거나 형식이 틀리면 실행 거부."""
    if not path:
        raise SystemExit("full/resume 모드는 --authorization-file 이 필요합니다(정식 승인 없이는 실행 불가).")
    p = Path(path)
    if not p.is_file():
        raise SystemExit(f"승인 파일 없음: {p}")
    data = json.loads(p.read_text(encoding="utf-8"))
    required = ["approving_org", "approval_date", "scope", "purpose", "approval_reference"]
    empty = [k for k in required if not str(data.get(k, "")).strip()]
    if empty:
        raise SystemExit("승인 파일 필수 항목 누락: " + ", ".join(empty))
    approved = dt.date.fromisoformat(data["approval_date"])
    if approved > dt.date.today():
        raise SystemExit("approval_date 가 미래 날짜입니다.")
    if data.get("expires_on") and dt.date.fromisoformat(data["expires_on"]) < dt.date.today():
        raise SystemExit("승인 기간이 만료되었습니다(expires_on).")
    if data.get("bulk_collection_allowed") is not True:
        raise SystemExit("승인 파일에 bulk_collection_allowed: true 가 없습니다.")
    return data


# ---------------- state (SQLite) ----------------

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS pages(page_number INTEGER PRIMARY KEY, status TEXT, row_count INTEGER,
  new_rows INTEGER, content_hash TEXT, url TEXT, raw_file TEXT, fetched_at TEXT, error TEXT);
CREATE TABLE IF NOT EXISTS terms(term_id TEXT PRIMARY KEY, {", ".join(f + " TEXT" for f in FIELDS)},
  detail_url TEXT, term_id_source TEXT, source_edition TEXT, source_url TEXT,
  page_number INTEGER, collected_at TEXT);
CREATE TABLE IF NOT EXISTS term_dupes(term_id TEXT, english_term TEXT, korean_term TEXT,
  first_page INTEGER, dup_page INTEGER, seen_at TEXT);
CREATE TABLE IF NOT EXISTS term_variants(term_id TEXT, english_term TEXT, variant_order INTEGER,
  korean_variant TEXT, original_korean TEXT, PRIMARY KEY(term_id, variant_order));
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
"""


def open_state(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def set_meta(conn, key, value):
    conn.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (key, json.dumps(value, ensure_ascii=False)))


def get_meta(conn, key, default=None):
    r = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return json.loads(r[0]) if r else default


def next_page(conn, start):
    """재개 지점: start 부터 처음으로 'ok' 가 아닌 페이지. 정상 페이지는 다시 받지 않는다."""
    page = start
    while conn.execute("SELECT 1 FROM pages WHERE page_number=? AND status='ok'", (page,)).fetchone():
        page += 1
    return page


# ---------------- fetch ----------------

def make_fetcher(cfg):
    import requests

    s = requests.Session()  # 쿠키/ASP 세션 유지
    s.headers["User-Agent"] = cfg["site"]["user_agent"]
    pg, th = cfg["pagination"], cfg["throttle"]
    url = cfg["site"]["base_url"].rstrip("/") + cfg["site"]["list_path"]

    def fetch(page):
        params = dict(pg.get("fixed_params") or {}, **{pg["page_param"]: page})
        for attempt in range(th["max_retries_5xx"] + 1):
            if pg["method"].upper() == "POST":
                r = s.post(url, data=params, timeout=th["timeout_sec"])
            else:
                r = s.get(url, params=params, timeout=th["timeout_sec"])
            if r.status_code in th["stop_status"]:
                raise StopCollect(f"HTTP {r.status_code} — 서버 거부 신호로 즉시 중단")
            if r.status_code >= 500 and attempt < th["max_retries_5xx"]:
                wait = th["backoff_base_sec"] * 2 ** attempt
                log.warning("HTTP %s page=%s, %ss 후 재시도(%s/%s)", r.status_code, page, wait,
                            attempt + 1, th["max_retries_5xx"])
                time.sleep(wait)
                continue
            r.raise_for_status()
            if not r.encoding or r.encoding.lower() == "iso-8859-1":
                r.encoding = r.apparent_encoding  # 레거시 ASP(EUC-KR) charset 미표기 대응
            return r.text, r.url
        raise RuntimeError("unreachable")

    return fetch


def check_robots(cfg):
    import requests

    if not cfg["site"].get("respect_robots_txt", True):
        return "skipped(by config)"
    robots_url = cfg["site"]["base_url"].rstrip("/") + "/robots.txt"
    r = requests.get(robots_url, timeout=cfg["throttle"]["timeout_sec"],
                     headers={"User-Agent": cfg["site"]["user_agent"]})
    if r.status_code == 404:
        return "robots.txt 없음(404)"
    r.raise_for_status()
    rp = urllib.robotparser.RobotFileParser()
    rp.parse(r.text.splitlines())
    target = cfg["site"]["base_url"].rstrip("/") + cfg["site"]["list_path"]
    if not rp.can_fetch(cfg["site"]["user_agent"], target):
        raise SystemExit(f"robots.txt 가 {target} 수집을 허용하지 않음 — 중단")
    return "allowed"


# ---------------- collect loop ----------------

def collect(cfg, conn, fetch, max_pages=None, sleep=time.sleep):
    """순차 수집. 빈 목록/동일 페이지 반복/마지막 페이지 도달 시 종료, 오류 시 실패 기록 후 중단."""
    start = cfg["pagination"]["start_page"]
    if get_meta(conn, "last_page_reached"):
        log.info("이미 마지막 페이지까지 수집됨(page=%s)", get_meta(conn, "last_page_reached"))
        return
    page = next_page(conn, start)
    prev = conn.execute("SELECT content_hash FROM pages WHERE page_number=? AND status='ok'",
                        (page - 1,)).fetchone()
    prev_hash = prev[0] if prev else None
    page_size, edition = cfg["pagination"].get("page_size"), cfg["parser"]["edition_label"]
    raw_dir = ROOT / cfg["paths"]["raw_dir"]
    raw_dir.mkdir(parents=True, exist_ok=True)
    done = 0

    while max_pages is None or done < max_pages:
        ts = now()
        try:
            html, url = fetch(page)
        except StopCollect as e:
            conn.execute("INSERT OR REPLACE INTO pages(page_number,status,fetched_at,error) VALUES(?,?,?,?)",
                         (page, "failed", ts, str(e)))
            conn.commit()
            log.error("page %s: %s", page, e)
            return
        except Exception as e:  # 네트워크/파싱 이전 오류: 건너뛰지 않고 중단 → resume 이 이 페이지부터 재시도
            conn.execute("INSERT OR REPLACE INTO pages(page_number,status,fetched_at,error) VALUES(?,?,?,?)",
                         (page, "failed", ts, f"{type(e).__name__}: {e}"))
            conn.commit()
            log.error("page %s 실패: %s", page, e)
            return

        raw_file = raw_dir / f"page_{page:05d}.html"
        raw_file.write_text(html, encoding="utf-8")
        try:
            rows, total = parse_list(html, cfg, url)
        except ValueError as e:
            conn.execute("INSERT OR REPLACE INTO pages VALUES(?,?,?,?,?,?,?,?,?)",
                         (page, "failed", 0, 0, None, url, str(raw_file), ts, f"parse: {e}"))
            conn.commit()
            log.error("page %s 파싱 실패: %s", page, e)
            return
        if total is not None:
            set_meta(conn, "site_total_count", total)
        h = hashlib.sha256("\n".join(r["term_id"] for r in rows).encode()).hexdigest()

        if not rows:
            conn.execute("INSERT OR REPLACE INTO pages VALUES(?,?,?,?,?,?,?,?,?)",
                         (page, "empty", 0, 0, h, url, str(raw_file), ts, None))
            set_meta(conn, "last_page_reached", page - 1)
            set_meta(conn, "stop_reason", "empty_list")
            conn.commit()
            log.info("page %s 빈 목록 → 마지막 페이지 %s", page, page - 1)
            return
        if h == prev_hash:
            conn.execute("INSERT OR REPLACE INTO pages VALUES(?,?,?,?,?,?,?,?,?)",
                         (page, "repeat", len(rows), 0, h, url, str(raw_file), ts,
                          "직전 페이지와 동일 내용(페이지 파라미터 무시 또는 마지막 페이지 반복)"))
            set_meta(conn, "last_page_reached", page - 1)
            set_meta(conn, "stop_reason", "repeated_page")
            conn.commit()
            log.warning("page %s 가 직전 페이지와 동일 → 중단", page)
            return

        new = 0
        for r in rows:
            vals = {f: r.get(f) for f in FIELDS}
            cur = conn.execute(
                f"INSERT OR IGNORE INTO terms VALUES(?,{','.join('?' * len(FIELDS))},?,?,?,?,?,?)",
                (r["term_id"], *vals.values(), r["detail_url"], r["term_id_source"], edition, url, page, ts))
            if cur.rowcount:
                new += 1
                for v in split_variants(r, cfg["parser"].get("variant_separator")):
                    conn.execute("INSERT OR IGNORE INTO term_variants VALUES(?,?,?,?,?)",
                                 (v["term_id"], v["english_term"], v["variant_order"],
                                  v["korean_variant"], v["original_korean"]))
            else:
                first = conn.execute("SELECT page_number FROM terms WHERE term_id=?", (r["term_id"],)).fetchone()[0]
                conn.execute("INSERT INTO term_dupes VALUES(?,?,?,?,?,?)",
                             (r["term_id"], r.get("english_term"), r.get("korean_term"), first, page, ts))
        conn.execute("INSERT OR REPLACE INTO pages VALUES(?,?,?,?,?,?,?,?,?)",
                     (page, "ok", len(rows), new, h, url, str(raw_file), ts, None))
        conn.commit()
        log.info("page %s: %s행 (신규 %s)", page, len(rows), new)
        done += 1
        prev_hash = h

        if page_size and total and page * page_size >= total:
            set_meta(conn, "last_page_reached", page)
            set_meta(conn, "stop_reason", "total_count_reached")
            conn.commit()
            log.info("사이트 전체 건수 %s 도달 → 종료", total)
            return
        page += 1
        if max_pages is None or done < max_pages:
            sleep(random.uniform(cfg["throttle"]["delay_min_sec"], cfg["throttle"]["delay_max_sec"]))


# ---------------- CLI ----------------

def state_path(cfg, mode):
    base = ROOT / cfg["paths"]["state_db"]
    return base.with_name(base.stem + "_sample.sqlite") if mode == "sample" else base


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mode", required=True, choices=["inspect", "sample", "full", "resume", "export"])
    ap.add_argument("--max-pages", type=int, default=SAMPLE_MAX_PAGES)
    ap.add_argument("--authorization-file")
    ap.add_argument("--config", default=str(ROOT / "config" / "settings.yaml"))
    ap.add_argument("--url", help="inspect 대상 URL(기본: settings 의 list URL)")
    a = ap.parse_args(argv)
    cfg = load_config(a.config)

    (ROOT / cfg["paths"]["log_dir"]).mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.StreamHandler(),
                                  logging.FileHandler(ROOT / cfg["paths"]["log_dir"] / "collect.log",
                                                      encoding="utf-8")])

    if a.mode == "inspect":
        from page_analyzer import inspect_site
        report = inspect_site(cfg, ROOT, a.url)
        print(json.dumps({k: report[k] for k in ("status", "url", "error") if k in report}, ensure_ascii=False))
        return 0 if report["status"] == "OK" else 2

    if a.mode == "export":
        from exporter import export
        db = state_path(cfg, "full")
        if not db.exists():
            db = state_path(cfg, "sample")
        if not db.exists():
            raise SystemExit("수집 상태 DB가 없습니다. sample 또는 full 을 먼저 실행하세요.")
        out = export(open_state(db), cfg, ROOT, source=db.name)
        print(json.dumps(out, ensure_ascii=False, indent=1))
        return 0

    if a.mode == "sample":
        if a.max_pages > SAMPLE_MAX_PAGES:
            raise SystemExit(f"sample 모드는 최대 {SAMPLE_MAX_PAGES}페이지까지만 허용")
        perm = None
    else:
        perm = load_permission(a.authorization_file)  # 승인 확인이 사이트 설정 확인보다 먼저
    require_site_config(cfg)
    log.info("robots.txt: %s", check_robots(cfg))

    db = state_path(cfg, a.mode)
    if a.mode == "sample":
        db.unlink(missing_ok=True)  # 샘플은 매번 처음 N페이지만 새로 받는다
    if a.mode == "full" and db.exists() and open_state(db).execute("SELECT COUNT(*) FROM pages").fetchone()[0]:
        raise SystemExit(f"기존 수집 상태가 있습니다({db}). 이어서 받으려면 --mode resume 을 쓰세요.")
    conn = open_state(db)
    set_meta(conn, "mode", a.mode)
    set_meta(conn, "permission", perm)
    set_meta(conn, "source_list_url", cfg["site"]["base_url"] + cfg["site"]["list_path"])
    set_meta(conn, "config_snapshot", {k: cfg[k] for k in ("pagination", "parser", "throttle")})
    conn.commit()
    collect(cfg, conn, make_fetcher(cfg), max_pages=a.max_pages if a.mode == "sample" else None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
