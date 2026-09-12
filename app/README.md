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

## 렌더 (v0.5)

```powershell
python -m dailyreels render
```

`data\edit_plan.json` → `output\DailyReel_YYYY-MM-DD.mp4` 1개를 만든다.
Renderer는 AI를 호출하지 않는다. edit_plan + 원본 영상 + FFmpeg만 사용한다.

| 옵션 | 설명 |
| --- | --- |
| `--plan PATH` | 다른 edit_plan 사용 |
| `--keep-temp` | `data\render_tmp` 세그먼트 보존 (렌더 실패 시에는 자동 보존) |
| `--no-preview` | QC용 preview 프레임 생략 |

동작:

- Hook clip을 맨 앞에 놓고, 본문에 같은 clip이 있으면 중복 제거
- 각 clip은 원본 중앙 구간 사용: `start = max(0, (source_duration - use_duration) / 2)`
  (`render.trim_mode = "plan"`으로 바꾸면 plan의 `start` 값을 사용)
- `use_duration`은 원본 길이로 clamp
- 1080x1920 / 30fps / H.264 / yuv420p / `+faststart`, 비율 유지 후 center crop (stretch 없음)
- FFmpeg autorotation만 사용 (rotation filter 중복 적용 안 함)
- 자막은 ASS로 생성해 burn-in. 본문은 `시간` + `caption` 2줄, Hook은 화면 중앙 큰 글씨 2줄
- 오디오는 기본 `mute` (`render.audio_mode = "original"`로 변경 가능)
- 기존 출력물은 덮어쓰지 않고 `_v02`, `_v03`으로 증가
- 렌더 후 ffprobe로 해상도/길이/코덱 재검증, `data\preview\`에 QC 스크린샷 3장

edit_plan.json 필드는 v0.4 출력 형태를 그대로 읽되 별칭을 허용한다:
`file|filename|source|path`, `duration|use_duration`, `time|time_label`, `caption|text`,
`source_duration`(없으면 ffprobe로 측정), `hook`(object / clip 참조 / 문자열).

자막 폰트는 `CAPTION_FONT_FILE` → `CAPTION_FONT_NAME` → config `preferred_fonts`
(Paperlogy → Malgun Gothic → Noto Sans KR → NanumGothic) 순으로 찾는다.
폰트 파일은 프로젝트로 복사하지 않고 설치된 위치를 그대로 참조한다.

## 테스트

```powershell
pytest -q
```

FFmpeg가 없는 환경에서는 실제 영상이 필요한 테스트(`test_ffprobe_integration.py`)가 skip된다.

## 상태

- v0.1 Scanner / Metadata — 완료
- v0.2~v0.4 Scene Analyzer / Story Planner (`edit_plan.json`) — 완료 (로컬 검증)
- v0.5 FFmpeg Renderer — 완료
- v0.6 one-command `dailyreels run` — 예정
