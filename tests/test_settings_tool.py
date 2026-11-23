"""대화로 설정을 바꾸는 도구. Phase 11.7.

여기서 지켜야 하는 것은 세 줄이다.

- **승인 없이는 안 바뀐다** — 조용한 변경은 아무도 설명 못 한다
- **비밀값은 대화로 안 바뀐다** — 대화 기록에 남으면 그게 유출이다
- **선언한 설정만 바뀐다** — 프롬프트·코드는 검증 없이 운영이 바뀐다
"""
from types import SimpleNamespace

import pytest

from vader.core import config, settings_tool


def _rune(*fields):
    return SimpleNamespace(
        manifest=SimpleNamespace(id="weather", required_fields=list(fields))
    )


def _field(name, kind="text", label="", default=""):
    return SimpleNamespace(name=name, kind=kind, label=label, default=default)


@pytest.fixture
def one_rune(monkeypatch):
    def install(*fields):
        monkeypatch.setattr(
            "vader.core.loader.discover", lambda *a, **k: ([_rune(*fields)], [])
        )

    return install


def test_nothing_changes_without_approval(one_rune, isolated_settings):
    """승인 통로가 없으면 **아무것도 안 한다.**"""
    one_rune(_field("city", label="기본 도시"))

    said = settings_tool.SettingsTool().invoke("weather", "city", "부산")

    assert "승인" in said
    assert config.rune_config("weather") == {}


def test_a_refusal_leaves_the_value_alone(one_rune, isolated_settings):
    one_rune(_field("city", label="기본 도시"))

    said = settings_tool.SettingsTool(ask=lambda *a: False).invoke(
        "weather", "city", "부산"
    )

    assert "승인하지 않아" in said
    assert config.rune_config("weather") == {}


def test_an_approved_change_lands_and_is_recorded(one_rune, isolated_settings):
    one_rune(_field("city", label="기본 도시", default="서울"))

    said = settings_tool.SettingsTool(ask=lambda *a: True).invoke(
        "weather", "city", "부산"
    )

    assert config.rune_config("weather")["city"] == "부산"
    assert "다음 실행부터" in said
    assert [(c.rune_id, c.before, c.after) for c in settings_tool.history()] == [
        ("weather", "서울", "부산")
    ]


def test_a_secret_is_never_changed_by_conversation(one_rune, isolated_settings):
    """키는 화면에서 받는다. 대화로 받으면 기록에 남는다."""
    one_rune(_field("client_secret", kind="secret", label="Client Secret"))
    asked = []

    said = settings_tool.SettingsTool(ask=lambda *a: asked.append(a) or True).invoke(
        "weather", "client_secret", "S3CRET"
    )

    assert "비밀값" in said
    assert asked == []
    assert config.rune_config("weather") == {}


def test_an_undeclared_field_is_refused(one_rune, isolated_settings):
    """룬이 선언한 것만 바꾼다 — 프롬프트도 코드도 아니다."""
    one_rune(_field("city"))

    said = settings_tool.SettingsTool(ask=lambda *a: True).invoke(
        "weather", "prompt", "새 프롬프트"
    )

    assert "없습니다" in said
    assert config.rune_config("weather") == {}


def test_a_number_field_is_stored_as_a_number(one_rune, isolated_settings):
    """`시작 쪽` 을 문자열로 저장하면 다음 실행이 터진다."""
    one_rune(_field("start_page", kind="number", default=1))

    settings_tool.SettingsTool(ask=lambda *a: True).invoke("weather", "start_page", "7")

    assert config.rune_config("weather")["start_page"] == 7
