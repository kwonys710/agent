# DailyReels

하루치 세로 영상들을 Instagram Reel 하나로 자동 편집한다.

원본 영상은 **읽기 전용**이다. Agent는 원본을 삭제/이동/이름변경/수정하지 않는다.

## 폴더 규칙

```
G:\내 드라이브\DailyReels
├─ IMG_001.mp4      ← 오늘 촬영본은 루트에 직접 복사 (하위 폴더 검색 안 함)
├─ app\             ← 이 코드
├─ data\            ← manifest.json, 분석 결과
├─ output\          ← 최종 Reel
└─ logs\
```

입력 대상: 루트에 직접 있는 `.mp4` `.mov` `.m4v`.
`app` `data` `output` `logs` 폴더는 제외된다.

## 설치

FFmpeg(`ffprobe` 포함)를 설치하고 PATH에 등록한다. PATH에 없으면 `.env`에 경로를 지정한다.

```powershell
cd "G:\내 드라이브\DailyReels\app"
py -3.13 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
copy .env.example .env
```

`.env`:

```env
DAILYREELS_ROOT=G:\내 드라이브\DailyReels
```

`DAILYREELS_ROOT`를 지정하지 않으면 `app\`의 상위 폴더를 루트로 본다.
설정값(확장자, 제외 폴더, timezone, caption_style 등)은 `config\dailyreels.toml`에 있다.

## 실행

```powershell
python -m dailyreels scan
```

루트의 영상을 ffprobe로 분석해 촬영시간순으로 정렬하고 `data\manifest.json`을 만든다.
`--root`로 루트를 덮어쓸 수 있고, `-v`로 상세 로그를 볼 수 있다.

출력 예:

```
DailyReels

Videos      17
Captured    07:03 ~ 23:41
Duration    02:47
Portrait    16
Landscape   1

Manifest created:
G:\내 드라이브\DailyReels\data\manifest.json
```

`captured_at`은 ffprobe `creation_time` 기준이며, 없으면 파일 수정시각으로 대체하고
`captured_at_source`에 `creation_time` / `file_mtime`을 기록한다.
읽지 못한 파일은 건너뛰고 manifest의 `errors`에 남는다.

## 테스트

```powershell
pytest -q
```

FFmpeg가 없는 환경에서는 실제 영상이 필요한 테스트(`test_ffprobe_integration.py`)가 skip된다.

## 상태

- v0.1 Scanner / Metadata — 완료
- v0.2 Scene Analyzer — 예정
- v0.3 Story Planner (`edit_plan.json`) — 예정
- v0.4 Renderer — 예정
