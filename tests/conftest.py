import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from engine.config.loader import DriveConfig, ProjectConfig
from engine.state.database import get_connection, init_db


@pytest.fixture()
def conn():
    connection = get_connection(":memory:")
    init_db(connection)
    yield connection
    connection.close()


@pytest.fixture()
def config():
    return ProjectConfig(
        project_id="jungsoo",
        project_name="테스트 프로젝트",
        drive=DriveConfig(
            drive_type="my_drive",
            root_folder_id="ROOT",
            include_subfolders=True,
            excluded_folder_ids=["EXCLUDED"],
        ),
        scope_config_version=1,
    )
