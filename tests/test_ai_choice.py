"""AI 연결 — 키를 넣으면 연결, 기본 AI 하나, 역할은 코어가 정한다 (2026-10-14 마스터 결정, codex1 합의).

지키려는 것: 키 값이 화면으로 나가지 않는다 · 연결 확인을 통과해야 연결된다 · 연결만 해 둔 다른 AI 로 자료가
자동으로 가지 않는다(문서 읽기만 예외) · 키를 바꾸다 설정 쓰기가 실패하면 옛 키로 돌아간다.
"""
import io
import json
import sys
import urllib.error

import pytest

if sys.platform != "win32":
    pytest.skip("Windows 보호 저장(DPAPI)은 Windows 에만 있다", allow_module_level=True)

from vader.core import ai_choice, config, secrets  # noqa: E402
from vader.core.http import HttpError  # noqa: E402
from vader.core.ocr.assign import choose  # noqa: E402
from vader.core.ocr.base import OcrProviderUnavailable  # noqa: E402

GEMINI_KEY, NVIDIA_KEY = "gem-평문-키-123", "nv-평문-키-456"


def write_api(path, api):
    data = json.loads(path.read_text(encoding="utf-8"))
    data["api"] = api
    data["place"] = {"name": "그대로 남아야 할 값"}
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    config.invalidate()


@pytest.fixture
def old_pc(isolated_settings, monkeypatch):
    """역할별 배정을 쓰던 PC (마스터의 지금 설정 모양). Gemini SDK 가 깔린 PC — CI 에는 없다."""
    from vader.core.ocr import gemini
    monkeypatch.setattr(gemini, "_import_genai", lambda: object())
    write_api(isolated_settings, {
        "ocr_provider": "gemini",
        "providers": {
            "gemini": {"api_key": GEMINI_KEY, "model": "gemini-2.5-flash", "daily_limit": 1400},
            "gptoss": {"base_url": "https://integrate.api.nvidia.com/v1", "api_key": NVIDIA_KEY,
                       "model": "openai/gpt-oss-120b", "daily_limit": 1000},
            "naver_search": {"key_id": "검색아이디", "key": "검색키"},
        },
        "roles": {"extract": {"provider": "gemini"}, "infer": {"providers": ["gptoss", "gemini"]}},
    })
    config.migrate_keys()
    return isolated_settings


@pytest.fixture
def new_pc(isolated_settings):
    """다른 PC — 아무것도 없다."""
    write_api(isolated_settings, {"default": None, "providers": {}})
    return isolated_settings


@pytest.fixture
def answers(monkeypatch):
    """연결 확인 응답. 기본은 성공. `answers.status = 410` 처럼 바꾼다."""
    class Box:
        status = 200
        sent = []
    box = Box()

    def post(url, body, headers, timeout=20):
        box.sent.append((url, body, headers))
        if box.status != 200:
            detail = json.dumps({"title": "Gone", "detail": f"end of life {headers}"}).encode()
            raise HttpError("실패") from urllib.error.HTTPError(url, box.status, "x", {}, io.BytesIO(detail))
        return {"choices": [{"message": {"content": "OK"}}]}
    monkeypatch.setattr(ai_choice, "_post", post)
    box.models = []                                         # 키로 쓸 수 있는 Gemini, 새것부터 — 기본은 못 물어봄
    monkeypatch.setattr(ai_choice, "_gemini_models", lambda key: box.models)
    return box


def test_plain_keys_move_to_protected_storage_and_leave_settings_and_backup(old_pc):
    text = old_pc.read_text(encoding="utf-8")
    assert GEMINI_KEY not in text and NVIDIA_KEY not in text and "검색키" in text
    assert GEMINI_KEY not in old_pc.with_name("settings.json.bak").read_text(encoding="utf-8")
    assert config.api_key("gemini") == GEMINI_KEY and config.api_key("gptoss") == NVIDIA_KEY


def test_role_settings_become_one_default_and_documents_go_to_a_reader(old_pc):
    assert ai_choice.migrate() and not ai_choice.migrate()     # 한 번만
    api = config.get("api")
    assert api["default"] == "gptoss" and "roles" not in api and "ocr_provider" not in api
    assert config.get("place.name") == "그대로 남아야 할 값"
    assert choose("infer") == "gptoss" and choose("chat") == "gptoss"
    assert choose("extract") == "gemini"                        # gpt-oss 는 문서를 못 읽는다


def test_the_screen_never_receives_a_key_and_shows_each_service_once(old_pc):
    ai_choice.migrate()
    shown = ai_choice.view()
    assert GEMINI_KEY not in json.dumps(shown, ensure_ascii=False)
    cards = {c["id"]: c for c in shown["cards"]}
    # NVIDIA 는 이미 gptoss 로 연결돼 있어 하나만, 검색 키는 없음. 로그인된 Claude Code·Codex 카드는 키 없이
    assert set(cards) == {"gemini", "gptoss", "claude", "claude_console", "codex_console", "agy_console"}
    assert cards["claude_console"]["keyless"] and not cards["gemini"].get("keyless")
    assert cards["gptoss"]["service"] == "NVIDIA" and cards["gptoss"]["recommended"].startswith("nvidia/")
    assert cards["claude"]["state"] == "off" and cards["gptoss"]["is_default"]
    # 이 앱이 확인한 모델이 아니다 — 도구 호출은 '확인 안 됨' 으로만 (codex1 지적)
    assert "대화로 설정 바꾸기 (확인 안 됨)" in cards["gptoss"]["can"] and "문서 읽기" not in cards["gptoss"]["can"]


def test_a_new_pc_connects_with_one_key_and_that_ai_becomes_the_default(new_pc, answers):
    assert ai_choice.view()["default"] is None and not ai_choice.ready()
    with pytest.raises(OcrProviderUnavailable, match="AI 연결"):
        choose("infer")
    result = ai_choice.connect("nvidia", key="nv-새-키")
    assert result["ok"] and result["default"] == "nvidia" and ai_choice.ready()
    assert config.api_key("nvidia") == "nv-새-키" and "nv-새-키" not in new_pc.read_text(encoding="utf-8")
    url, body, headers = answers.sent[0]
    assert url == "https://integrate.api.nvidia.com/v1/chat/completions"
    assert body["model"] == ai_choice.PRESETS["nvidia"]["model"]
    with pytest.raises(OcrProviderUnavailable):
        choose("extract")                                       # 문서를 읽는 AI 가 없다 — 몰래 다른 데로 가지 않는다


def test_a_failed_check_saves_nothing_new(new_pc, answers):
    answers.status = 401
    result = ai_choice.connect("nvidia", key="틀린-키")
    assert not result["ok"] and result["message"].startswith("키가 맞지 않아요") and "틀린-키" not in result["message"]
    assert config.get("api.providers") == {} and secrets.get(config.secret_name("nvidia")) is None


def test_a_retired_model_needs_action_and_the_recommended_model_fixes_it(old_pc, answers):
    ai_choice.migrate()
    answers.status = 410
    result = ai_choice.connect("gptoss")
    assert not result["ok"] and result["message"].startswith("이 모델은 제공이 끝났어요")
    card = {c["id"]: c for c in result["cards"]}["gptoss"]
    assert card["state"] == "fix"
    with pytest.raises(OcrProviderUnavailable, match="기본 AI 를 쓸 수 없습니다"):
        choose("infer")                                         # 다른 연결된 AI(Gemini)로 몰래 넘기지 않는다

    answers.status = 200
    fixed = ai_choice.connect("gptoss", model=card["recommended"])
    assert fixed["ok"] and choose("infer") == "gptoss"
    assert config.get("api.providers.gptoss.model") == card["recommended"]


def test_default_and_disconnect(old_pc, answers):
    ai_choice.migrate()
    assert ai_choice.set_default("gemini")["default"] == "gemini"
    with pytest.raises(ai_choice.ChoiceError):
        ai_choice.set_default("claude")                         # 연결 안 된 것은 기본이 못 된다
    shown = ai_choice.disconnect("gemini")
    # 기본을 끊으면 기본은 비운다 — 남은 AI(gptoss)를 몰래 올리지 않는다
    assert shown["default"] is None and "gemini" not in config.get("api.providers")
    assert secrets.get(config.secret_name("gemini")) is None
    with pytest.raises(OcrProviderUnavailable, match="기본 AI 가 없습니다"):
        choose("extract")


def test_a_settings_write_failure_puts_the_old_key_back(old_pc, answers, monkeypatch):
    ai_choice.migrate()
    monkeypatch.setattr(config, "_write", lambda data: (_ for _ in ()).throw(OSError("디스크 가득")))
    with pytest.raises(OSError):
        ai_choice.connect("gptoss", key="새-키")
    assert config.api_key("gptoss") == NVIDIA_KEY


def test_a_secret_round_trips(isolated_settings):
    secrets.put("x", "값")
    assert secrets.get("x") == "값"
    secrets.delete("x")
    assert secrets.get("x") is None


def test_a_wrong_new_key_does_not_break_a_working_card(old_pc, answers):
    ai_choice.migrate()
    answers.status = 401
    result = ai_choice.connect("gptoss", key="틀린-새-키")
    assert not result["ok"]
    assert {c["id"]: c for c in result["cards"]}["gptoss"]["state"] == "on"
    assert config.api_key("gptoss") == NVIDIA_KEY and choose("infer") == "gptoss"


def test_documents_move_only_when_the_default_cannot_read(old_pc, answers):
    ai_choice.migrate()
    ai_choice.set_default("gemini")
    _put("gemini", "checked", {"ok": False, "model": "gemini-2.5-flash"})
    with pytest.raises(OcrProviderUnavailable, match="기본 AI 를 쓸 수 없습니다"):
        choose("extract")                                       # 막힌 것이지 못 읽는 게 아니다 — 넘기지 않는다


def _put(name, field, value):
    raw = json.loads(json.dumps(config.load()))
    raw["api"]["providers"][name][field] = value
    config._write(raw)
    config.invalidate()


def test_a_model_added_to_a_connection_shares_its_key_and_waits_for_the_star(new_pc, answers):
    ai_choice.connect("nvidia", key="nv-키")
    result = ai_choice.add_model("nvidia", "openai/gpt-oss-20b")
    assert result["ok"], result["message"]
    new = config.get("api.providers.nvidia-2")
    assert new["connection"] == "nvidia" and new["base_url"] == ai_choice.PRESETS["nvidia"]["base_url"]
    assert config.api_key("nvidia-2") == "nv-키" and secrets.get(config.secret_name("nvidia-2")) is None
    assert answers.sent[-1][2]["Authorization"] == "Bearer nv-키" and answers.sent[-1][1]["model"] == "openai/gpt-oss-20b"
    assert config.get("api.default") == "nvidia" and choose("infer") == "nvidia"   # 별을 옮겨야 쓴다
    assert ai_choice.set_default("nvidia-2")["default"] == "nvidia-2" and choose("infer") == "nvidia-2"
    with pytest.raises(ai_choice.ChoiceError, match="이미 있는"):
        ai_choice.add_model("nvidia-2", "openai/gpt-oss-20b")
    with pytest.raises(ai_choice.ChoiceError):
        ai_choice.add_model("gemini", "x")                        # SDK 연결·없는 연결에는 아직 못 더한다

    ai_choice.disconnect("nvidia")                              # 모델 카드만 — 키는 nvidia-2 가 쓴다
    assert config.api_key("nvidia-2") == "nv-키" and choose("infer") == "nvidia-2"
    ai_choice.disconnect("nvidia-2")                            # 마지막 카드 — 이제 키도 지운다
    assert secrets.get(config.secret_name("nvidia")) is None


def test_a_failed_model_add_makes_no_card(new_pc, answers):
    ai_choice.connect("nvidia", key="nv-키")
    answers.status = 404
    result = ai_choice.add_model("nvidia", "없는-모델")
    assert not result["ok"] and set(config.get("api.providers")) == {"nvidia"}


def test_a_new_key_on_a_shared_connection_unchecks_its_other_models(new_pc, answers):
    ai_choice.connect("nvidia", key="nv-키")
    ai_choice.add_model("nvidia", "openai/gpt-oss-20b")
    ai_choice.connect("nvidia-2", key="nv-새-키")
    assert config.api_key("nvidia") == "nv-새-키"
    assert "checked" not in config.get("api.providers.nvidia")    # 옛 키로 한 확인이다


def test_the_daily_limit_belongs_to_the_key(new_pc, answers):
    from vader.core.ocr.assign import _look

    class Ledger:
        def calls_on(self, provider=None):
            return {"nvidia": 600, "nvidia-2": 500}.get(provider, 0)
    ai_choice.connect("nvidia", key="nv-키")
    ai_choice.add_model("nvidia", "openai/gpt-oss-20b")
    assert _look("nvidia-2", Ledger(), "infer").calls_today == 1100
    assert not _look("nvidia-2", Ledger(), "infer").usable    # 한도 1000 을 함께 쓴다


def test_saving_waits_for_the_guard(new_pc, answers):
    from contextlib import contextmanager

    @contextmanager
    def busy():
        raise ValueError("실행 중에는 바꿀 수 없습니다")
        yield
    with pytest.raises(ValueError, match="실행 중"):
        ai_choice.connect("nvidia", key="nv-키", guard=busy)
    assert config.get("api.providers") == {}


def test_a_key_alone_finds_a_gemini_model_the_key_can_use(new_pc, answers, monkeypatch):
    """새 키에서 고정 모델이 막히면 그 키로 쓸 수 있는 Flash 를 새것부터 시험해 처음 답한 것으로 맞춘다 (2026-10-21 새 PC)."""
    def post(url, body, headers, timeout=20):
        if "gemini-2.5-flash" in url:
            raise HttpError("실패") from urllib.error.HTTPError(url, 404, "x", {}, io.BytesIO(b"{}"))
        if "gemini-3.8-flash" in url:
            raise HttpError("timed out")                    # 가장 새 것이 몰려서 느리다
        return {}
    monkeypatch.setattr(ai_choice, "_post", post)
    answers.models = ["gemini-3.8-flash", "gemini-3.6-flash", "gemini-3.5-flash"]

    assert ai_choice.connect("gemini", key=GEMINI_KEY)["ok"]
    assert config.get("api.providers.gemini.model") == "gemini-3.6-flash"
    assert ai_choice.connect("gemini", model="gemini-2.5-flash")["ok"] is False   # 사람이 고른 모델은 바꾸지 않는다


def test_a_logged_in_console_takes_documents_without_a_key_and_never_becomes_the_default(old_pc, answers, monkeypatch):
    """키 없이 연결 확인 → 문서 읽기 담당. 대화를 못 해 기본 AI 는 될 수 없다. 막히면 다른 AI 로 넘기지 않는다."""
    from vader.core.ocr import console

    monkeypatch.setattr(console.shutil, "which", lambda name: f"C:/bin/{name}.cmd")
    monkeypatch.setattr(console.ConsoleProvider, "complete", lambda self, prompt, schema: "OK")

    def no_chat(self, messages):
        raise OcrProviderUnavailable("대화 확인 실패")
    monkeypatch.setattr(console.ConsoleProvider, "chat", no_chat)
    ai_choice.migrate()
    default = config.get("api.default")
    result = ai_choice.connect("claude_console")
    assert result["ok"] and config.get("api.documents") == "claude_console"
    assert config.get("api.default") == default, "기본 AI 는 그대로 — 판단·대화는 원래 AI 가 맡는다"
    assert choose("extract") == "claude_console" and choose("infer") == default
    # 모델 이름을 잘못 넣어 확인이 실패해도 쓰던 모델은 그대로 (codex1 지적)
    from vader.core.ocr.base import OcrProviderError

    def broken(self, prompt, schema):
        raise OcrProviderError("모델을 찾지 못했습니다")
    monkeypatch.setattr(console.ConsoleProvider, "complete", broken)
    assert not ai_choice.connect("claude_console", model="오타-모델")["ok"]
    assert config.get("api.providers.claude_console.model") == "sonnet"
    assert config.get("api.providers.claude_console.checked.ok") is True
    with pytest.raises(ai_choice.ChoiceError, match="기본 AI 가 될 수 없"):
        ai_choice.set_default("claude_console")   # 대화 확인을 통과하지 못했다
    monkeypatch.setattr(console.shutil, "which", lambda name: None)       # 프로그램이 사라졌다
    with pytest.raises(OcrProviderUnavailable, match="문서 읽기를 맡은"):
        choose("extract")
    ai_choice.disconnect("claude_console")
    assert config.get("api.documents") is None


def test_a_logged_in_console_that_passes_the_chat_check_can_take_the_star(old_pc, answers, monkeypatch):
    """대화 확인까지 통과하면 별(기본 AI) — 판단·대화가 그쪽으로. 문서 읽기 담당은 따로 그대로. 모델을 바꾸면 다시 확인 전."""
    from vader.core.ocr import console

    monkeypatch.setattr(console.shutil, "which", lambda name: f"C:/bin/{name}.cmd")
    monkeypatch.setattr(console.ConsoleProvider, "complete", lambda self, prompt, schema: "OK")
    monkeypatch.setattr(console.ConsoleProvider, "chat", lambda self, messages: "네")
    monkeypatch.setattr(console, "codex_limits", lambda: [{"window_mins": 300, "used_percent": 7, "resets_at": None}])
    ai_choice.migrate()
    result = ai_choice.connect("codex_console")
    card = next(c for c in result["cards"] if c["id"] == "codex_console")
    assert card["can_default"] and "대화" in card["can"] and not any("설정 바꾸기" in x for x in card["can"])
    assert card["usage"]["windows"][0]["used_percent"] == 7
    ai_choice.set_default("codex_console")
    assert choose("chat") == "codex_console" and choose("infer") == "codex_console"
    assert choose("extract") == "codex_console" and config.get("api.documents") == "codex_console"
    monkeypatch.setattr(console.ConsoleProvider, "chat", lambda self, messages: (_ for _ in ()).throw(OcrProviderUnavailable("x")))
    ai_choice.connect("codex_console")                  # 같은 모델 재확인 — 문서는 통과, 대화는 실패
    with pytest.raises(OcrProviderUnavailable, match="대화 확인"):
        choose("chat")                                  # 별이 남아도 대화에는 고르지 않는다
    assert choose("extract") == "codex_console", "문서 읽기는 그대로"
    monkeypatch.setattr(console.ConsoleProvider, "chat", lambda self, messages: "네")
    ai_choice.connect("codex_console")
    monkeypatch.setattr(console.ConsoleProvider, "chat", lambda self, messages: (_ for _ in ()).throw(OcrProviderUnavailable("x")))
    ai_choice.connect("codex_console", model="gpt-다른모델")
    assert config.get("api.providers.codex_console.model") == "", "별을 단 카드는 대화가 안 되는 새 모델로 바꾸지 않는다"
    ai_choice.set_default("gemini")
    ai_choice.connect("codex_console", model="gpt-다른모델")
    card = next(c for c in ai_choice.view()["cards"] if c["id"] == "codex_console")
    assert card["model"] == "gpt-다른모델" and not card["can_default"], "새 모델은 대화 확인을 다시 통과해야 별을 받는다"
