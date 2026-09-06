"""프로젝트별 config.yaml 로더.

Google Drive Folder ID 등 프로젝트별 값은 코드에 하드코딩하지 않고
projects/<project_id>/config.yaml 또는 환경변수에서 읽는다.
"""
from __future__ import annotations

import dataclasses
import os
from pathlib import Path
from typing import Optional

import yaml

PROJECTS_DIR = Path(__file__).resolve().parents[2] / "projects"

PLACEHOLDER_VALUES = {"", "CHANGE_ME", None}


class ConfigError(RuntimeError):
    """config.yaml이 없거나, 필수 값이 비어 있거나, 형식이 잘못된 경우."""


@dataclasses.dataclass(frozen=True)
class DriveConfig:
    drive_type: str  # "my_drive" | "shared_drive"
    root_folder_id: str
    include_subfolders: bool
    excluded_folder_ids: list[str]
    shared_drive_id: Optional[str] = None


@dataclasses.dataclass(frozen=True)
class ProjectConfig:
    project_id: str
    project_name: str
    drive: DriveConfig
    scope_config_version: int


def _resolve_root_folder_id(project_id: str, raw_value: Optional[str]) -> str:
    if raw_value not in PLACEHOLDER_VALUES:
        return raw_value  # type: ignore[return-value]

    env_key = f"PM_{project_id.upper()}_ROOT_FOLDER_ID"
    env_value = os.environ.get(env_key)
    if env_value:
        return env_value

    raise ConfigError(
        f"'{project_id}' 프로젝트의 drive.root_folder_id가 설정되지 않았습니다. "
        f"projects/{project_id}/config.yaml의 drive.root_folder_id를 채우거나 "
        f"환경변수 {env_key}를 설정하세요."
    )


def load_project_config(project_id: str) -> ProjectConfig:
    config_path = PROJECTS_DIR / project_id / "config.yaml"
    if not config_path.exists():
        raise ConfigError(f"프로젝트 설정 파일을 찾을 수 없습니다: {config_path}")

    with config_path.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    try:
        drive_raw = raw["drive"]
    except KeyError as exc:
        raise ConfigError(f"config.yaml에 'drive' 섹션이 없습니다: {config_path}") from exc

    root_folder_id = _resolve_root_folder_id(project_id, drive_raw.get("root_folder_id"))

    drive = DriveConfig(
        drive_type=drive_raw.get("drive_type", "my_drive"),
        root_folder_id=root_folder_id,
        include_subfolders=bool(drive_raw.get("include_subfolders", True)),
        excluded_folder_ids=list(drive_raw.get("excluded_folder_ids") or []),
        shared_drive_id=drive_raw.get("shared_drive_id"),
    )

    scope_raw = raw.get("scope") or {}

    try:
        project_id_in_file = raw["project_id"]
    except KeyError as exc:
        raise ConfigError(f"config.yaml에 'project_id'가 없습니다: {config_path}") from exc

    if project_id_in_file != project_id:
        raise ConfigError(
            f"디렉터리명({project_id})과 config.yaml의 project_id({project_id_in_file})가 일치하지 않습니다."
        )

    return ProjectConfig(
        project_id=project_id_in_file,
        project_name=raw.get("project_name", project_id_in_file),
        drive=drive,
        scope_config_version=int(scope_raw.get("config_version", 1)),
    )
