"""테스트는 리포 루트의 진짜 settings.json 을 절대 읽지 않는다.

그 파일은 API 키와 사업자등록번호가 들어 있어 깃에 올라가지 않는다. 즉
CI 체크아웃에는 없다. 테스트가 그걸 읽으면 로컬에서만 통과하고 CI 에서는
FileNotFoundError 로 무더기 실패한다. 여기서 빈 설정으로 갈아끼워
두 곳이 같은 것을 보게 한다.
"""
from __future__ import annotations

import pytest

from vader.core import config


@pytest.fixture(autouse=True)
def isolated_settings(tmp_path_factory, monkeypatch):
    # 테스트 자신의 tmp_path 밖에 둔다. 안에 두면 그 폴더를 glob 해서 파일을
    # 세거나 하위 폴더를 룬으로 훑는 테스트가 이 파일까지 집어삼킨다.
    path = tmp_path_factory.mktemp("config") / "settings.json"
    # 작업 폴더도 갈아끼운다. 기본값이 `~/.vaderai` 라서, 여기 안 적으면
    # 테스트가 **마스터의 진짜 홈 폴더에 기록을 남긴다.** 실제로 남겼다 —
    # 설정 변경 이력 21줄이 테스트에서 나온 것이었다.
    import json

    path.write_text(
        json.dumps({"processing": {"data_dir": str(path.parent / "data")}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "SETTINGS_PATH", path)
    monkeypatch.setattr(config, "_cache", None)
    monkeypatch.setattr(config, "HOME", path.parent / "home")
    monkeypatch.setattr(config, "REQUIRE_WORKSPACE", False)      # 진입점을 부르는 테스트가 켜도 다음 테스트로 새지 않게
    monkeypatch.setattr(config, "_workspace", None)
    monkeypatch.delenv(config.WORKSPACE_ENV, raising=False)   # 기기 층(로그인 토큰·device.json)도 진짜 홈 밖으로
    yield path
    config.invalidate()
