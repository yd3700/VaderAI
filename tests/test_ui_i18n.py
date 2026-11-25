"""앱 화면 영어 사전 (2026-11-24 영문판 2단계) — 사전이 깨지지 않았는지와 언어 고르기."""
import json
import re
from pathlib import Path

from vader.core import config

ASSETS = Path(__file__).resolve().parent.parent / "vader" / "desktop" / "assets"


def _dictionary() -> dict:
    body = (ASSETS / "i18n-en.js").read_text(encoding="utf-8")
    return json.loads(body[body.index("{"): body.rindex("}") + 1])


def test_every_translation_keeps_the_same_placeholders():
    holes = lambda s: sorted(re.findall(r"\{\d+\}", s))   # noqa: E731
    bad = [k for k, v in _dictionary().items() if v and holes(k) != holes(v)]
    assert bad == []


def test_pages_load_the_dictionary_before_their_scripts():
    for page in ("index.html", "gate.html", "badge.html"):
        html = (ASSETS / page).read_text(encoding="utf-8")
        first = html.index("<script")
        assert html[first:].startswith('<script src="i18n-en.js"></script><script src="i18n.js"></script>'), page


def test_language_follows_the_device_setting_then_windows(monkeypatch):
    monkeypatch.setattr(config, "device_get", lambda key, default=None: "en" if key == "ui.lang" else default)
    assert config.ui_lang() == "en"
    monkeypatch.setattr(config, "device_get", lambda key, default=None: "fr" if key == "ui.lang" else default)
    assert config.ui_lang() in ("ko", "en")          # 모르는 값은 무시하고 Windows 표시 언어


def test_every_core_message_shown_on_screen_has_english():
    """코어가 화면에 보내는 문장(raise·message 값)마다 영어가 있다 — 원문을 고치고 사전을 잊으면 여기서 걸린다 (4단계)."""
    import ast
    import importlib.util

    spec = importlib.util.spec_from_file_location("i18n_core_extract", Path(__file__).resolve().parent.parent / "tools" / "i18n_core_extract.py")
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    found = set().union(*(tool.strings(ast.parse((tool.ROOT / m).read_text(encoding="utf-8"))) for m in tool.MODULES))
    assert sorted(found - set(_dictionary())) == []
