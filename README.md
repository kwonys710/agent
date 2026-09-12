> 이 저장소에는 두 프로젝트가 있다. DailyReels는 `app/`에 있다 → [app/README.md](app/README.md)

# PM Automation Engine — MVP 1단계 (Google Drive Incremental Scan)

이번 단계의 목적은 하나다:

> Google Drive에서 프로젝트 대상 파일의 변경사항만 증분으로 감지하고,
> 기존 파일 전체를 매번 다시 읽지 않는 구조가 실제로 동작하는지 검증한다.

Slack, WBS, 이메일, Claude 분석, Daily Todo, Weekly Report는 **이번 단계에 포함되지 않는다.**
이 코드베이스는 Drive Changes API 기반 Bootstrap/Incremental과 SQLite Registry만 구현한다.

## 디렉터리 구조

```
engine/
  config/loader.py      프로젝트별 config.yaml 로더
  state/                SQLite 스키마/연결(트랜잭션) 헬퍼
  drive/
    client.py           Drive API 클라이언트 (GoogleDriveClient) — metadata 조회만 제공, content download 메서드 없음
    models.py           DriveFileMeta, mimeType 분류(binary/google_native/folder)
    scope.py            Project Scope Filter (Folder Registry 캐시 기반)
    registry.py         files/folders/processing_events 테이블 공용 쓰기 헬퍼
    bootstrap.py         Bootstrap Mode (최초 1회)
    incremental.py       Incremental Mode (일상 자동 실행)
projects/jungsoo/config.yaml   프로젝트별 설정 (Drive Folder ID 등)
scripts/drive_bootstrap.py     CLI: Bootstrap 실행
scripts/drive_incremental.py   CLI: Incremental 실행
tests/                          pytest, FakeDriveClient로 실제 Drive 접근 없이 검증
data/pm_automation.db           SQLite Machine State (git에 포함하지 않음)
```

## 설치

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Google OAuth 인증 준비 (실제 Drive 연동 시)

1. Google Cloud Console에서 OAuth Desktop App 클라이언트를 생성하고 `client_secret.json`을 다운로드한다.
2. 이 저장소 밖(또는 `.gitignore`에 포함된 `credentials/`)에 보관한다. **git에 커밋하지 않는다.**
3. `.env.example`을 `.env`로 복사하고 `GOOGLE_OAUTH_CLIENT_SECRET_FILE` 경로를 지정한다.
4. `projects/jungsoo/config.yaml`의 `drive.root_folder_id`를 실제 프로젝트 폴더 ID로 채운다
   (또는 환경변수 `PM_JUNGSOO_ROOT_FOLDER_ID`로 지정).

> 이 세션(원격 컨테이너)에는 브라우저 기반 OAuth 동의 화면을 완료할 수단이 없어
> 실제 Google 계정 인증은 수행하지 않았다. 아래 자동 테스트는 전부 `FakeDriveClient`로
> 실제 네트워크 호출 없이 로직만 검증한다. 실제 Drive 연동은 사용자의 로컬 환경에서
> 위 절차대로 진행해야 한다.

## 실행

```bash
# 최초 1회만 (이미 실행되어 있으면 --override 없이는 거부됨)
python scripts/drive_bootstrap.py --project jungsoo

# 매일 자동 실행 대상
python scripts/drive_incremental.py --project jungsoo
```

## 테스트

```bash
pytest -v
```

모든 테스트는 실제 Google Drive에 접근하지 않고 `tests/fakes.py`의 `FakeDriveClient`로 동작한다.
`FakeDriveClient`에는 파일 content를 반환하는 메서드가 애초에 존재하지 않는다 — Bootstrap/Incremental
어느 경로에서도 content download가 코드 구조상 불가능함을 이렇게 보장한다.
