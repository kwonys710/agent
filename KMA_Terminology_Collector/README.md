# KMA Terminology Collector

대한의사협회 의학용어 검색(term.kma.org `search/list.asp`) 결과를 **승인받은 범위 안에서** 순차 수집해
영문/한글 용어를 CSV·XLSX로 정리하는 도구입니다.

> **현재 상태 (2026-11-06):** 국내 PC에서 실제 구조를 분석해 `config/settings.yaml` 을 채웠다
> (6.1판, GET `pageno`, `dl.searchList`). 브라우저 저장본 3페이지로 실제 데이터 샘플을 만들었다.
> **term.kma.org robots.txt 가 `Disallow: /` 이므로 sample/full/resume 은 robots 확인에서 중단된다.**
> 전체 데이터는 공식 DB를 요청해야 한다(`../KMA_의학용어집_6판_조사/05_최종보고/KMA_6판_공식자료요청_초안.md`).
> 근거: `reports/실사이트_분석_검증보고서.md`. 이전 국외 환경 기록: `reports/검증보고서.md`.
>
> ```bash
> # 브라우저에서 직접 열람·저장한 p1.html, p2.html … 변환 (사이트에 요청하지 않음)
> python src/kma_collect.py --mode import --html-dir samples/a_substring
> python src/kma_collect.py --mode export
> ```
> 결과: `data/output/KMA_의학용어집_6.1판_REAL_SAMPLE_import.csv/.xlsx`

## 설치

```bash
pip install -r requirements.txt
python -m playwright install chromium   # inspect 용 브라우저(이미 있으면 생략)
```

## 사용 순서

1. **구조 분석** — 국내망 PC에서 실행합니다.
   ```bash
   python src/kma_collect.py --mode inspect
   ```
   `reports/inspect_report.json` 과 `data/raw/inspect_page1.html`, `inspect_page2.html` 이 생성됩니다.
   - `page1.forms` : 검색어 필드, 판본 선택(select options), 폼 action/method
   - `page1.tables[].headers`, `sample_rows` : 결과 테이블 헤더와 예시 행
   - `page2_navigation.request` : '2' 링크를 실제로 클릭했을 때 발생한 요청(method, URL, post_data)
   - `page1.download_links` : 엑셀/다운로드 메뉴가 있으면 여기에 나옵니다 → **있다면 그 공식 파일을 우선 사용**
   - `robots_txt`

   검색어 입력 후에만 목록이 나오는 구조라면 브라우저에서 검색한 결과 URL을 `--url` 로 넘겨 다시 실행합니다.

2. **설정 확정** — `config/settings.yaml` 의 null 값을 inspect 에서 관찰한 값으로 채웁니다.
   `pagination.method/page_param/fixed_params`, `parser.row_selector`, `parser.header_map`,
   `total_count_regex`, `term_id_query_param`. 화면에서 제6판이 확인되면 `edition_label: "제6판"`.
   복수 대응어 구분자는 원본에서 명확할 때만 `variant_separator` 에 지정합니다(기본: 분리 안 함).

3. **샘플(최대 3페이지)**
   ```bash
   python src/kma_collect.py --mode sample --max-pages 3
   python src/kma_collect.py --mode export
   ```
   `data/output/` 결과와 `reports/validation_report.json` 을 화면과 대조해 파서를 확인합니다.

4. **전체 수집 — 정식 승인 후에만**
   `permission.example.json` 을 `permission.json` 으로 복사해 승인 내용을 적고
   `bulk_collection_allowed: true` 로 바꿉니다(필수 항목이 비어 있거나 만료되면 실행 거부).
   ```bash
   python src/kma_collect.py --mode full   --authorization-file permission.json
   python src/kma_collect.py --mode resume --authorization-file permission.json   # 중단 후 이어받기
   python src/kma_collect.py --mode export
   ```

## 동작 규칙

| 항목 | 동작 |
|---|---|
| 요청 | 순차 1개, 요청 간 3~5초 무작위 대기, `requests.Session` 으로 쿠키/세션 유지 |
| 403 / 429 | 즉시 중단, 해당 페이지 `failed` 기록 |
| 5xx | 최대 3회 지수 백오프(5·10·20초) 후 중단 |
| robots.txt | 대상 경로가 Disallow 면 실행 거부 |
| 종료 감지 | 빈 목록 / 직전 페이지와 동일 내용 / `page × page_size ≥ 전체 건수` |
| 재개 | SQLite(`data/processed/state.sqlite`)에서 처음으로 `ok` 가 아닌 페이지부터. 정상 페이지는 다시 받지 않음 |
| 중복 | `term_id`(사이트 ID 우선, 없으면 영문+한글 해시) 기준. 중복은 버리지 않고 `term_dupes` 에 기록 |
| 정규화 | 태그 제거, 공백·줄바꿈 정리만. 쉼표·괄호를 구분자로 쓰지 않음. 원본에 없는 컬럼은 출력하지 않음. layout=dl 은 원문 내부 공백도 보존(`1. 유산  2. 낙태`) |
| dd 표지 | `[동]`→korean_alternative(한글)/english_synonym(영문), `[약]`→abbreviation, `[원]`·미확인 표지→notes 에 원문 그대로 |
| 검색 조건 유지 | 사이트 페이지 링크는 `spelling_chk` 를 빠뜨려 조건이 바뀜 → `fixed_params` 를 매 요청에 보냄. 페이지 간 총건수가 바뀌면 `failed` 기록 후 중단 |
| 복수대응어 | `variant_separator`(정규식)로 분리해 복수대응어 시트에만 기록. korean_term 은 원문 |
| 금지 | 로그인·CAPTCHA·차단 우회, 프록시/IP 회전, 동시 요청 — 구현하지 않음 |

## 출력

- `data/output/KMA_의학용어집_<판본>.csv` — UTF-8 BOM, 영문 기준 정렬 (sample/import 는 `_REAL_SAMPLE_<mode>` 접미사)
- `data/output/KMA_의학용어집_<판본>.xlsx` — 의학용어_전체 / 복수대응어 / 중복검증 / 수집페이지_이력 / 오류및누락 / 수집메타데이터
- `reports/validation_report.json` — PASS / FAIL / WARN / BLOCKED
- `reports/synthetic_demo/` — **합성 데이터로 만든 출력 형식 예시**(실제 KMA 데이터 아님)

## 테스트

```bash
python -m unittest discover -s tests -v
```

- `tests/test_pipeline.py` (8건): 합성 HTML로 페이지 순회·중복 제거·재개·429 중단·CSV/XLSX·승인 게이트
- `tests/test_real_html.py` (7건): 실제 term.kma.org HTML로 파싱·표지·원문 공백·복수대응어·import·검색 조건 변경 감지·누락 페이지·robots
  - 실제 KMA 데이터(`samples/`, `tests/fixtures/`, `data/output/*.csv|xlsx`)는 저작권 고지(사전 승인 없는 재배포 금지)에 따라 **저장소에 올리지 않는다**(.gitignore). 파일이 없으면 이 7건은 skip 된다. 브라우저에서 열람·저장한 페이지를 `samples/a_substring/p1~3.html` 에 두면 실행된다.
