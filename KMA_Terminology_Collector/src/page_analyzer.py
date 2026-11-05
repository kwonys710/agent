"""사이트 구조 분석(--mode inspect). 실제 HTML/네트워크 요청만 기록하고, 접속 실패 시 실패 사실만 남긴다."""
import json
import os
import re
from pathlib import Path

DOM_PROBE = r"""() => {
  const txt = e => (e.innerText || e.textContent || '').replace(/\s+/g,' ').trim();
  const forms = [...document.forms].map(f => ({
    name: f.name, id: f.id, action: f.getAttribute('action'), method: (f.method||'').toUpperCase(),
    onsubmit: f.getAttribute('onsubmit'),
    fields: [...f.elements].map(el => ({tag: el.tagName.toLowerCase(), type: el.type, name: el.name, id: el.id,
      value: el.type === 'password' ? '' : el.value,
      options: el.tagName === 'SELECT' ? [...el.options].map(o => ({value:o.value, text:txt(o), selected:o.selected})) : undefined}))
  }));
  const tables = [...document.querySelectorAll('table')].map((t, i) => ({
    index: i, id: t.id, class: t.className,
    headers: [...t.querySelectorAll('th')].map(txt),
    row_count: t.querySelectorAll('tr').length,
    sample_rows: [...t.querySelectorAll('tr')].slice(0, 4).map(r => [...r.children].map(txt)),
  })).filter(t => t.row_count > 1);
  const links = [...document.querySelectorAll('a, button, input[type=button], img[onclick]')].map(a => ({
    text: txt(a) || a.value || a.alt || '', href: a.getAttribute('href'), onclick: a.getAttribute('onclick')
  }));
  const paging = links.filter(l => /^\d+$/.test(l.text) || /다음|이전|next|prev|끝|처음|last|first|»|›|>>/i.test(l.text)
    || /page|goPage|movePage|paging/i.test((l.href||'') + (l.onclick||'')));
  const downloads = links.filter(l => /엑셀|excel|xls|csv|다운로드|download|자료실|DB/i.test(l.text + (l.href||'')));
  const scripts = [...document.scripts].map(s => s.src || (s.textContent||'').slice(0, 4000));
  return {title: document.title, forms, tables, paging_links: paging, download_links: downloads,
          body_text_head: txt(document.body).slice(0, 3000), inline_scripts: scripts,
          edition_mentions: (txt(document.body).match(/[^ ]{0,20}(제\s*\d\s*판|\d\s*판|edition)[^ ]{0,20}/gi) || []).slice(0, 30)};
}"""


def _browser_path():
    p = os.environ.get("CHROMIUM_PATH", "/opt/pw-browsers/chromium")
    return p if Path(p).exists() else None


def inspect_site(cfg, root, url=None):
    from playwright.sync_api import sync_playwright

    report_dir, raw_dir = root / cfg["paths"]["report_dir"], root / cfg["paths"]["raw_dir"]
    report_dir.mkdir(parents=True, exist_ok=True)
    raw_dir.mkdir(parents=True, exist_ok=True)
    base = cfg["site"]["base_url"].rstrip("/")
    candidates = [url] if url else [base + cfg["site"]["list_path"],
                                    base.replace("https://", "http://") + cfg["site"]["list_path"]]
    report = {"status": "BLOCKED", "attempts": [], "requests": []}

    with sync_playwright() as pw:
        kw = {"executable_path": _browser_path()} if _browser_path() else {}
        browser = pw.chromium.launch(**kw)
        page = browser.new_page(user_agent=cfg["site"]["user_agent"])
        page.on("request", lambda r: report["requests"].append(
            {"method": r.method, "url": r.url, "type": r.resource_type, "post_data": r.post_data})
            if r.resource_type in ("document", "xhr", "fetch") else None)
        resp = None
        for u in candidates:
            try:
                resp = page.goto(u, timeout=cfg["throttle"]["timeout_sec"] * 1000, wait_until="domcontentloaded")
                report["attempts"].append({"url": u, "status": resp.status if resp else None})
                if resp and resp.ok:
                    report["url"] = page.url
                    break
            except Exception as e:  # 접속 실패는 사실 그대로 기록
                report["attempts"].append({"url": u, "error": f"{type(e).__name__}: {str(e).splitlines()[0]}"})
            resp = None

        if resp is None:
            report["error"] = "모든 후보 URL 접속 실패 — 구조를 추정하지 않음"
        else:
            report["status"] = "OK"
            (raw_dir / "inspect_page1.html").write_text(page.content(), encoding="utf-8")
            report["page1"] = page.evaluate(DOM_PROBE)
            report["cookies"] = [{"name": c["name"], "domain": c["domain"]} for c in page.context.cookies()]
            report["page2_navigation"] = _try_page2(page, report, raw_dir, cfg)
            try:
                r = page.request.get(base + "/robots.txt")
                report["robots_txt"] = {"status": r.status, "body": r.text()[:2000]}
            except Exception as e:
                report["robots_txt"] = {"error": str(e).splitlines()[0]}
        browser.close()

    report["next_steps"] = ("page2_navigation.request 의 method/파라미터와 page1.tables[].headers 를 보고 "
                            "config/settings.yaml 의 pagination/parser 값을 채운 뒤 --mode sample 실행")
    (report_dir / "inspect_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    return report


def _try_page2(page, report, raw_dir, cfg):
    """'2' 페이지 링크를 실제로 클릭하고 발생한 문서 요청을 기록(파라미터 추측 대신 관찰)."""
    link = page.locator("a", has_text=re.compile(r"^\s*2\s*$")).first
    if link.count() == 0:
        return {"status": "NOT_FOUND", "detail": "텍스트가 '2' 인 페이지 링크 없음(검색어 입력이 먼저 필요할 수 있음)"}
    before = len(report["requests"])
    try:
        with page.expect_navigation(timeout=cfg["throttle"]["timeout_sec"] * 1000):
            link.click()
    except Exception as e:
        return {"status": "FAIL", "detail": str(e).splitlines()[0], "requests": report["requests"][before:]}
    (raw_dir / "inspect_page2.html").write_text(page.content(), encoding="utf-8")
    docs = [r for r in report["requests"][before:] if r["type"] == "document"]
    return {"status": "OK", "url_after": page.url, "request": docs[-1] if docs else None,
            "tables_after": page.evaluate(DOM_PROBE)["tables"]}
