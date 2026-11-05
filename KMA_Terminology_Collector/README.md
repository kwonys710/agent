# KMA Terminology Collector

대한의사협회 의학용어 검색(term.kma.org `search/list.asp`) 결과를 **승인받은 범위 안에서** 순차 수집해
영문/한글 용어를 CSV·XLSX로 정리하는 도구입니다.

> 현재 상태: 사이트 구조 분석 **BLOCKED**(개발 환경에서 접속 불가). 사이트별 설정값은 비어 있고,
> 실제 구조를 확인하기 전에는 sample/full/resume 이 실행되지 않습니다. 자세한 내용은 `reports/검증보고서.md`.

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
| 정규화 | 태그 제거, 공백·줄바꿈 정리만. 쉼표·괄호를 구분자로 쓰지 않음. 원본에 없는 컬럼은 출력하지 않음 |
| 금지 | 로그인·CAPTCHA·차단 우회, 프록시/IP 회전, 동시 요청 — 구현하지 않음 |

## 출력

- `data/output/KMA_의학용어집_6판.csv` — UTF-8 BOM, 영문 기준 정렬
- `data/output/KMA_의학용어집_6판.xlsx` — 의학용어_전체 / 복수대응어 / 중복검증 / 수집페이지_이력 / 오류및누락 / 수집메타데이터
- `reports/validation_report.json` — PASS / FAIL / WARN / BLOCKED
- `reports/synthetic_demo/` — **합성 데이터로 만든 출력 형식 예시**(실제 KMA 데이터 아님)

## 테스트

```bash
python -m unittest discover -s tests -v
```

합성 HTML로 페이지 순회·중복 제거·재개·429 중단·CSV/XLSX·승인 게이트를 검증합니다.
실제 사이트 파서 검증은 inspect → sample 로 따로 해야 합니다.
