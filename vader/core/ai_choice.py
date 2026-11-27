"""AI 연결 — 서비스 카드에 키를 넣고, 기본 AI 하나에 별을 찍는다 (2026-10-14 마스터 결정).

마스터: "다른 PC 에서는 아무것도 없을 거야. 문서읽기/판단/대화를 사용자가 넣으라고 하면 그건 또 다른 업무야.
심플하게, Orca 의 에이전트 등록·사용을 모티브로." codex1 합의 (`wiki/handoff/ai-simple-codex.md`):

- 사람이 하는 일은 둘: 서비스에 **키 넣기**(연결 확인을 통과해야 연결된다) · **기본 AI 하나**. 역할 배정 화면은 없다.
- 어느 역할에 무엇을 쓸지는 코어 정책(`ocr/assign.py:choose`) — 기본 AI, 문서 읽기만 문서를 읽는 연결된 AI 로.
  연결만 해 둔 다른 AI 로 자료를 자동으로 보내지 않는다.
- 모델·주소는 코어가 검증해 둔 권장값(`PRESETS`). 사람은 고급에서만 바꾼다. 권장값이 바뀌어도 쓰던 모델을 몰래 바꾸지 않는다.
- 키는 화면에 돌려주지 않는다. 키를 바꿀 때 설정 쓰기가 실패하면 옛 키로 되돌린다(codex1 지적: 순서 문제).
- 연결 확인은 업무 자료 없는 짧은 요청 한 번. 연결됨은 "이 키·모델로 답이 왔다" 까지이지 모든 업무 보증이 아니다.

AI 추가 (codex1 합의 `wiki/handoff/ai-model-add-codex.md`): **연결(주소·키) 과 모델 카드를 나눈다.**
카드 하나 = `api.providers.<id>`. 같은 연결에 더한 모델은 `connection` 으로 그 연결의 키를 가리킨다 — 키를 복사하지 않고,
같은 호스트라는 이유로 키를 다시 쓰지도 않는다(사람이 그 연결을 직접 고를 때만). 별(기본)은 모델 카드에 찍는다.
더한 모델은 기본이 되지 않는다. 카드를 지우면 그 카드만, 키는 그 연결을 쓰는 마지막 카드가 지워질 때 지운다.
"""
from __future__ import annotations

import json
import re
import urllib.error
from contextlib import nullcontext
from copy import deepcopy
from datetime import datetime
from urllib.parse import urlparse

from vader.core import config, secrets
from vader.core.http import HttpError, HttpGateway
from vader.core.ocr import provider_factory
from vader.core.ocr.assign import choose

#: 코어가 검증해 둔 서비스. `checked_on` 이 없으면 아직 이 앱에서 확인하지 못한 권장값이다.
#: NVIDIA 는 2026-10-14 에 판단(JSON)·대화 도구 호출까지 확인했다 (gpt-oss-120b 는 2026-09-03 종료).
PRESETS = {
    "gemini": {"service": "Google Gemini", "model": "gemini-2.5-flash", "checked_on": "2026-10-14",
               "key_help": "aistudio.google.com 에서 API 키 발급"},
    "claude": {"service": "Anthropic Claude", "model": "claude-haiku-4-5",
               "key_help": "console.anthropic.com 에서 API 키 발급"},
    # 키 없이 이 PC 에 로그인된 프로그램 (vader/core/ocr/console.py, 2026-10-29 문서 읽기 → 2026-11-27 글 대화도).
    # 연결하면 '문서 처리' 를 맡고, 대화 확인을 통과한 Claude Code·Codex 는 기본 AI(별)도 될 수 있다. Antigravity 는 문서·대화 모두 닫힘.
    "claude_console": {"service": "Claude Code (로그인)", "model": "sonnet", "keyless": True,
                       "models": ["sonnet", "haiku", "opus", "fable"],
                       "key_help": "이 PC 에 로그인된 Claude Code 로 문서를 읽고 대화해요. API 키 없이 그 계정의 사용량을 써요."},
    "agy_console": {"service": "Antigravity (로그인)", "model": "", "keyless": True,
                    "key_help": "이 PC 에 로그인된 Antigravity CLI 로 문서를 읽어요. API 키 없이 그 계정의 사용량을 써요."},
    "codex_console": {"service": "Codex (로그인)", "model": "", "keyless": True,
                      "key_help": "이 PC 에 로그인된 Codex 로 문서를 읽고 대화해요. API 키 없이 그 계정의 사용량을 써요."},
    "nvidia": {"service": "NVIDIA", "base_url": "https://integrate.api.nvidia.com/v1",
               "model": "nvidia/nemotron-3-super-120b-a12b", "checked_on": "2026-10-14", "daily_limit": 1000,
               "key_help": "build.nvidia.com 에서 API 키 발급"},
}
_HOSTS = {"integrate.api.nvidia.com": "NVIDIA", "api.openai.com": "OpenAI"}
TEST_PROMPT = "연결 확인입니다. OK 한 단어로만 답하세요."
CAN = {"extract": "문서 읽기", "infer": "판단", "chat": "대화"}


class ChoiceError(ValueError):
    """사람에게 그대로 보여 줄 수 있는 거절."""


# ── 보기 ──

def view() -> dict:
    """서비스 카드들. **키 값은 넣지 않는다.** 연결된 것 먼저, 아직 안 한 권장 서비스는 뒤에."""
    default = config.get("api.default")
    names = _ai_providers()
    cards = [_card(name, default) for name in names]
    hosts = {_host(n) for n in names}
    for preset_id, preset in PRESETS.items():
        taken = preset_id in names or (preset.get("base_url") and _host_of(preset["base_url"]) in hosts)
        if not taken:
            cards.append({"id": preset_id, "service": preset["service"], "label": preset["service"], "state": "off",
                          "state_text": "미연결", "model": preset["model"], "can": _can_kind(preset_id, preset),
                          "key_help": preset["key_help"], "is_default": False, "recommended": "",
                          "keyless": bool(preset.get("keyless")), "models": preset.get("models", [])})
    return {"cards": cards, "default": default if default in names else None,
            "documents": _safe_choose("extract"), "thinking": _safe_choose("infer")}


def _card(name: str, default) -> dict:
    block = config.get(f"api.providers.{name}", {}) or {}
    factory = provider_factory(name)
    model = str(block.get("model", ""))
    checked = block.get("checked") or {}
    preset = _preset_for(name)
    if not _has_key(name):
        state, text = "off", "미연결"
    elif not factory().is_available():
        state, text = "fix", ("이 PC 에 " + _service(name).split(" (")[0] + " 가 설치되어 있지 않아요" if _keyless(name)
                              else "이 PC 에 필요한 부품이 없어요 (pip install "
                              + ("anthropic" if name == "claude" else "google-genai") + ")")
    elif checked.get("ok") is False and checked.get("model") == model:
        state, text = "fix", checked.get("message") or "연결 확인에 실패했어요"
    else:
        state, text = "on", "연결됨" + (" · " + checked["at"][:10] + " 확인" if checked.get("ok") else " · 확인 전")
    recommended = preset.get("model", "") if preset and preset.get("model") != model else ""
    conn = config.connection_of(name)
    # 도구 호출은 모델마다 다르다 — 이 앱이 확인한 모델에서만 '된다' 고 말한다 (codex1 지적).
    verified = bool(preset and preset.get("checked_on") and preset.get("model") == model)
    tools = [] if preset_is_sdk(name) or _keyless(name) else ["대화로 설정 바꾸기" + ("" if verified else " (확인 안 됨)")]
    return {"id": name, "service": (preset or {}).get("service") or _service(name), "label": label_for(name),
            "state": state, "state_text": text, "model": model, "can": [CAN[r] for r in CAN if factory().supports(r)]
            + tools, "is_default": name == default, "connection": conn, "shared": len(_cards_on(conn)),
            "can_add": state != "off" and not preset_is_sdk(name) and bool(block.get("base_url")),
            "key_help": (preset or {}).get("key_help", ""), "recommended": recommended, "keyless": _keyless(name),
            "documents": config.get("api.documents") == name, "can_default": state == "on" and _can_default(name),
            "models": (preset or {}).get("models", []) if _keyless(name) else [],
            "usage": _usage(name) if state == "on" else None}


def _can_default(name: str) -> bool:
    """별을 받을 수 있나 — 키 AI 는 연결되면, 로그인 프로그램은 **이 모델로 대화 확인**을 통과했을 때만."""
    if not _keyless(name):
        return True
    block = config.get(f"api.providers.{name}", {}) or {}
    checked = block.get("checked") or {}
    return bool(provider_factory(name)().supports("chat") and checked.get("ok") and checked.get("chat")
                and checked.get("model") == block.get("model"))


def _usage(name: str) -> dict | None:
    """구독 사용량 — Codex 만 공식 수단으로 읽는다. 못 읽으면 None(화면은 '—'). Claude Code 는 안내만."""
    if name == "claude_console":
        from vader.core.ocr import console
        windows, seen = console.claude_limits()
        return {"windows": windows, "seen": seen or None,
                "note": "" if windows else "이 앱에서 한 번 부르면 보여요 · Claude Code 에서 /usage 로도 볼 수 있어요"}
    if name != "codex_console":
        return None
    from vader.core.ocr import console
    return {"windows": console.codex_limits(), "note": ""}


def label_for(name: str) -> str:
    """'NVIDIA · nemotron-3-super-120b-a12b' 처럼 — 접속 서비스와 모델을 섞지 않는다."""
    block = config.get(f"api.providers.{name}", {}) or {}
    model = str(block.get("model", "")).split("/")[-1]
    service = _service(name)
    return f"{service} · {model}" if model else service


# ── 바꾸기 ──

def connect(name: str, key: str | None = None, model: str | None = None, guard=nullcontext) -> dict:
    """연결 확인을 통과하면 저장한다. 실패하면 새 설정은 남기지 않는다(이미 있던 AI 는 '조치 필요' 로 표시).

    `key` 가 없으면 이미 저장된 키로 다시 확인한다 — 모델을 바꾸거나 권장 모델로 옮길 때.
    `guard` 는 저장하는 짧은 순간을 감싼다 — 확인(네트워크) 동안 작업이 시작됐으면 저장을 막는다(화면이 넘긴다).
    """
    asked = model
    existing = config.get(f"api.providers.{name}")
    preset = PRESETS.get(name)
    if existing is None and preset is None:
        raise ChoiceError(f"모르는 AI 입니다: {name}")
    if existing is not None and name not in _ai_providers():
        raise ChoiceError(f"AI 가 아닌 항목입니다: {name}")
    block = deepcopy(existing) if existing is not None else {
        k: v for k, v in preset.items() if k in ("base_url", "model", "daily_limit")}
    model = str(model or block.get("model") or (preset or {}).get("model", "")).strip()
    if (not model and not _keyless(name)) or len(model) > 200:
        raise ChoiceError("모델 이름을 적어 주세요.")
    if _keyless(name):
        block.pop("keyless", None)
        result = _check_console(name, model)
        block["checked"] = {"ok": result["ok"], "at": datetime.now().isoformat(timespec="seconds"), "model": model,
                            "message": "" if result["ok"] else result["message"], "chat": bool(result.get("chat"))}
        block["model"] = model
        # 새 모델 확인이 실패하면 쓰던 모델을 덮지 않는다 — 같은 모델 재확인 실패만 '조치 필요' (codex1 지적)
        # 기본 AI(별)인 카드는 새 모델이 대화 확인까지 통과해야 바꾼다 — 별은 남고 대화만 깨지는 일이 없게
        usable = result["ok"] and (result.get("chat") or config.get("api.default") != name)
        if usable or (existing is not None and model == existing.get("model")):
            with guard():
                _save(name, block, None, documents=result["ok"])
        return {**result, **view()}
    key = (key or "").strip() or None
    use_key = key or _stored_key(name)
    if not use_key:
        raise ChoiceError("키를 넣어 주세요.")
    result = _check(name, block.get("base_url", ""), model, use_key)
    if not result["ok"] and name == "gemini" and not asked:
        # 키만 넣었다 — 그 키로 쓸 수 있는 Flash 를 새것부터, 짧게 기다리며 시험해 처음 답한 것을 쓴다.
        # 가장 새 모델이 몰려서 느린 날이 있다 (2026-10-21: 3.8·3.7 은 20초 넘게, 3.6 은 3초).
        for candidate in [m for m in _gemini_models(use_key) if m != model][:4]:
            tried = _check(name, "", candidate, use_key, timeout=8)
            if tried["ok"]:
                model, result = candidate, tried
                break
    block["model"] = model
    block["checked"] = {"ok": result["ok"], "at": datetime.now().isoformat(timespec="seconds"), "model": model,
                        "message": "" if result["ok"] else result["message"]}
    if not result["ok"]:
        # 저장된 키·쓰던 모델로 실패했을 때만 '조치 필요'. 새 키가 틀린 것은 멀쩡한 카드를 바꾸지 않는다.
        if existing is not None and key is None and model == existing.get("model"):
            with guard():
                _save(name, block, None)
        return {**result, **view()}
    with guard():
        _save(name, block, key, make_default=not config.get("api.default"))
    return {**result, **view()}


def add_model(source: str, model: str, guard=nullcontext) -> dict:
    """이미 연결된 서비스(`source` 카드의 연결)에 모델 하나를 더한다. 키는 그 연결의 것 — 다시 묻지 않는다.

    주소는 그 연결에서 가져온다(사람이 적지 않는다). 연결 확인을 통과해야 카드가 생긴다. 기본은 그대로.
    지금은 OpenAI 호환 연결만 — Gemini·Claude 는 이름이 코드에 박혀 있어 모델 하나뿐이다(다음 단계).
    """
    block = config.get(f"api.providers.{source}")
    if source not in _ai_providers() or preset_is_sdk(source) or not (block or {}).get("base_url"):
        raise ChoiceError("모델을 더할 수 있는 연결이 아닙니다.")
    conn = config.connection_of(source)
    use_key = _stored_key(source)
    if not use_key:
        raise ChoiceError("먼저 이 서비스에 키를 넣어 연결하세요.")
    model = str(model or "").strip()
    if not model or len(model) > 200:
        raise ChoiceError("모델 이름을 적어 주세요.")
    if any(config.get(f"api.providers.{n}.model") == model for n in _cards_on(conn)):
        raise ChoiceError("이 연결에 이미 있는 모델입니다.")
    result = _check(source, block["base_url"], model, use_key)
    if not result["ok"]:
        return {**result, **view()}
    taken = set(config.get("api.providers", {}) or {}) | set(PRESETS) | {"default"}
    new = next(f"{conn}-{i}" for i in range(2, 1000) if f"{conn}-{i}" not in taken)
    fresh = {"base_url": block["base_url"], "model": model, "connection": conn,
             "daily_limit": block.get("daily_limit", 1000),
             "checked": {"ok": True, "at": datetime.now().isoformat(timespec="seconds"), "model": model, "message": ""}}
    with guard():
        _save(new, fresh, None)
    return {**result, "message": f"{label_for(new)} 을(를) 더했어요. 쓰려면 별을 옮기세요.", **view()}


def set_default(name: str) -> dict:
    if _keyless(name) and not _can_default(name):
        raise ChoiceError("이 AI 는 대화 확인을 통과하지 않아 기본 AI 가 될 수 없어요. [연결 확인] 을 다시 눌러 보세요.")
    if name not in _ai_providers() or not _has_key(name):
        raise ChoiceError("연결된 AI 만 기본으로 고를 수 있습니다.")
    data = deepcopy(config.load())
    data.setdefault("api", {})["default"] = name
    config._write(data)
    return view()


def disconnect(name: str) -> dict:
    """카드를 지운다. 키는 그 연결을 쓰는 마지막 카드일 때만 지운다(서비스 계정의 키를 폐기하는 것은 아니다).

    지운 카드가 기본이었으면 기본은 **비운다** — 다른 서비스를 몰래 기본으로 올리면 자료가 사람이 고르지 않은
    곳으로 간다 (codex1 지적). 지난 실행 기록은 그대로.
    """
    if name not in _ai_providers():
        raise ChoiceError(f"연결된 AI 가 아닙니다: {name}")
    conn = config.connection_of(name)
    last = _cards_on(conn) == [name]
    data = deepcopy(config.load())
    api = data["api"]
    del api["providers"][name]
    if api.get("default") == name:
        api["default"] = None
    if api.get("documents") == name:
        # 사람이 끊었다 — 문서 읽기는 기본 정책(기본 AI → 문서를 읽는 연결된 AI)으로 돌아가고,
        # 화면의 '문서 처리: …' 가 다음에 받을 AI 를 보여 준다. 막혔을 때 몰래 넘기는 것과는 다르다.
        api["documents"] = None
    config._write(data)   # 설정이 먼저 — 새 호출이 이 AI 를 못 고르게 한 뒤 키를 지운다
    if last and secrets.available():
        secrets.delete(config.secret_name(conn))
    return view()


def migrate() -> bool:
    """역할별 배정(roles) 설정을 '기본 AI 하나' 로 옮긴다. 한 번만. 옮겼으면 참.

    기본 = 판단(infer) 첫 AI → 대화 첫 AI → 문서 제공자 순으로 있는 것. 문서 읽기는 정책이 알아서 고른다.
    """
    api = config.get("api", {}) or {}
    if "default" in api:
        return False
    names = [n for n in _ai_providers() if not _keyless(n)]   # 대화를 못 하는 카드는 기본이 될 수 없다
    order = []
    for role in ("infer", "chat"):
        order += config.get(f"api.roles.{role}.providers") or [config.get(f"api.roles.{role}.provider")]
    order.append(config.get("api.ocr_provider"))
    default = next((n for n in order if n in names), names[0] if names else None)
    data = deepcopy(config.load())
    data.setdefault("api", {})["default"] = default
    for old in ("roles", "ocr_provider"):
        data["api"].pop(old, None)
    config._write(data)
    return True


# ── 안쪽 ──

def _save(name: str, block: dict, key: str | None, make_default: bool = False, documents: bool = False) -> None:
    """키 → 설정 순서로 쓰되, 설정 쓰기가 실패하면 키를 되돌린다 (codex1 지적)."""
    data = deepcopy(config.load())
    api = data.setdefault("api", {})
    api.setdefault("providers", {})[name] = block
    block.pop("api_key", None)
    if make_default:
        api["default"] = name
    if documents:
        api["documents"] = name
    conn = block.get("connection") or name
    secret = config.secret_name(conn)
    if key is not None:   # 키가 바뀌면 같은 연결의 다른 모델은 '확인 전' 으로 — 옛 키로 한 확인이다
        for other in _cards_on(conn):
            if other != name:
                api["providers"][other].pop("checked", None)
    before = None
    if key is not None:
        if not secrets.available():
            raise ChoiceError("이 PC 에서는 키를 보호 저장할 수 없습니다.")
        before = secrets.get(secret)
        secrets.put(secret, key)
    try:
        config._write(data)
    except Exception:
        if key is not None:
            secrets.put(secret, before) if before else secrets.delete(secret)
        raise


def _check(name: str, base_url: str, model: str, api_key: str, timeout: float = 20) -> dict:
    try:
        _ask(name, base_url, model, api_key, timeout)
    except HttpError as exc:
        refused = exc.__cause__
        if isinstance(refused, urllib.error.HTTPError):
            # 풀어 읽은 뒤에 가린다 — JSON 안에서는 키가 \uXXXX 로 적혀 있어 먼저 가리면 빠져나간다.
            detail = _short(refused.read().decode("utf-8", "replace")[:2000]).replace(api_key, "***")
            why = {401: "키가 맞지 않아요.", 403: "이 키로는 쓸 수 없어요.", 404: "모델을 찾지 못했어요.",
                   410: "이 모델은 제공이 끝났어요 — 권장 모델로 바꾸세요.", 429: "사용 한도에 걸렸어요."
                   }.get(refused.code, f"거절됐어요 (HTTP {refused.code}).")
            return {"ok": False, "message": f"{why} {detail}".strip()}
        if "timed out" in str(exc):
            return {"ok": False, "message": "시간 안에 답이 오지 않았어요. 잠시 뒤 다시 확인해 주세요."}
        return {"ok": False, "message": "접속하지 못했어요. 인터넷 연결을 확인해 주세요."}
    except ValueError:
        return {"ok": False, "message": "답이 왔지만 읽을 수 없는 모양이에요. 모델 이름을 확인해 주세요."}
    # 서버가 돌려준 글자는 보여 주지 않는다 — 키를 되돌려 보내는 서버도 있을 수 있다 (codex1 지적).
    return {"ok": True, "message": "연결됐어요."}


def _ask(name: str, base_url: str, model: str, api_key: str, timeout: float) -> None:
    """가장 짧은 텍스트 요청. 답이 JSON 으로 오면 연결된 것이다 — 내용은 보지 않는다(생각하는 모델은 빈 답도 준다).
    표준 라이브러리 통로만 — SDK 가 없는 PC 에서도 확인된다."""
    if name == "gemini":
        _post(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
              {"contents": [{"parts": [{"text": TEST_PROMPT}]}], "generationConfig": {"maxOutputTokens": 8}},
              {"x-goog-api-key": api_key}, timeout)
    elif name == "claude":
        _post("https://api.anthropic.com/v1/messages",
              {"model": model, "max_tokens": 8, "messages": [{"role": "user", "content": TEST_PROMPT}]},
              {"x-api-key": api_key, "anthropic-version": "2023-06-01"}, timeout)
    else:
        _post(f"{base_url.rstrip('/')}/chat/completions",
              {"model": model, "max_tokens": 16, "messages": [{"role": "user", "content": TEST_PROMPT}]},
              {"Authorization": f"Bearer {api_key}"}, timeout)


def _gemini_models(api_key: str) -> list[str]:
    """이 키로 쓸 수 있는 정식 Flash, 새것부터. 못 물어보면 빈 목록.

    Gemini 모델은 차례로 은퇴하고 새 키부터 막힌다 (2026-10-21, 새 PC 에서 gemini-2.5-flash 거절).
    이름을 고정해 두면 그때마다 앱을 고쳐야 한다.
    """
    try:
        listed = HttpGateway(retry_max=1).get(
            "https://generativelanguage.googleapis.com/v1beta/models?pageSize=1000",
            headers={"x-goog-api-key": api_key}).json()
    except Exception:
        return []
    found = []
    for item in listed.get("models", []):
        match = re.fullmatch(r"models/(gemini-(\d+(?:\.\d+)?)-flash)", str(item.get("name", "")))
        if match and "generateContent" in item.get("supportedGenerationMethods", []):
            found.append((tuple(int(n) for n in match[2].split(".")), match[1]))
    return [name for _, name in sorted(found, reverse=True)]


def _post(url: str, body: dict, headers: dict, timeout: float = 20) -> dict:
    """코어의 나가는 문(`vader/core/http.py`)으로 — 새 문을 만들지 않는다. 연결 확인이라 다시 보내지 않는다."""
    return HttpGateway(retry_max=1, timeout=timeout).post(url, json=body, headers=headers).json()


def _short(detail: str) -> str:
    """오류 본문에서 사람이 읽을 한 줄 — OpenAI 모양(error.message)과 문제 보고 모양(detail·title) 둘 다."""
    try:
        parsed = json.loads(detail)
    except ValueError:
        return detail[:160]
    error = parsed.get("error", parsed) if isinstance(parsed, dict) else parsed
    if isinstance(error, dict):
        error = error.get("message") or error.get("detail") or error.get("title") or error
    return str(error)[:200]


def ready() -> bool:
    """판단·대화에 쓸 AI 가 있나. 코어 화면의 '연결해 주세요' 안내가 쓴다."""
    return _safe_choose("infer") is not None


def _safe_choose(role: str):
    try:
        return choose(role)
    except Exception:
        return None


def _cards_on(conn: str) -> list[str]:
    """그 연결(키)을 쓰는 카드들."""
    return [n for n in _ai_providers() if config.connection_of(n) == conn]


def _ai_providers() -> list[str]:
    """AI 로 쓸 수 있는 제공자만 — 검색 키 같은 다른 항목은 섞지 않는다."""
    names = (config.get("api.providers", {}) or {}).keys()
    return [n for n in names if not n.startswith("_") and provider_factory(n) is not None]


def _preset_for(name: str) -> dict | None:
    if name in PRESETS:
        return PRESETS[name]
    host = _host(name)
    return next((p for p in PRESETS.values() if p.get("base_url") and _host_of(p["base_url"]) == host), None)


def preset_is_sdk(name: str) -> bool:
    """SDK 어댑터(Gemini·Claude)인가. 대화로 설정 바꾸기(도구 호출)는 지금 OpenAI 호환 연결에서만 된다 (`chat.py`)."""
    return name in ("gemini", "claude")


def _can_kind(preset_id: str, preset: dict) -> list[str]:
    if preset.get("keyless"):
        return [CAN[r] for r in CAN if provider_factory(preset_id)().supports(r)]
    if preset_is_sdk(preset_id):
        return [CAN["extract"], CAN["infer"], CAN["chat"]]
    return [CAN["infer"], CAN["chat"], "대화로 설정 바꾸기"]


def _service(name: str) -> str:
    return {"gemini": "Google Gemini", "claude": "Anthropic Claude"}.get(name) or _HOSTS.get(_host(name)) or _host(name) or name


def _host(name: str) -> str:
    return _host_of(str(config.get(f"api.providers.{name}.base_url", "") or ""))


def _host_of(url: str) -> str:
    return urlparse(url).hostname or ""


def _stored_key(name: str) -> str:
    try:
        return config.api_key(name)
    except ValueError:
        return ""


def _has_key(name: str) -> bool:
    if _keyless(name):
        return name in (config.get("api.providers", {}) or {})
    return bool(_stored_key(name))


def _keyless(name: str) -> bool:
    return bool((PRESETS.get(name) or {}).get("keyless"))


def _check_console(name: str, model: str) -> dict:
    """키 대신 그 프로그램에 짧은 질문 하나 — 설치·로그인이 됐는지. 업무 자료는 보내지 않는다."""
    from vader.core.ocr import provider_factory
    from vader.core.ocr.base import OcrError

    provider = provider_factory(name)(model=model or None)
    if not provider.is_available():
        return {"ok": False, "message": f"이 PC 에 {_service(name).split(' (')[0]} 가 설치되어 있지 않아요."}
    try:   # 문서 읽기 길(JSON 답) 확인 — 대화 확인과 따로 센다 (codex1 next3 ②)
        answer = provider.complete(TEST_PROMPT, str)
    except OcrError as exc:
        return {"ok": False, "message": _short(str(exc))}
    if not isinstance(answer, str):
        return {"ok": False, "message": "답이 왔지만 읽을 수 없는 모양이에요."}
    if not provider.supports("chat"):
        return {"ok": True, "message": "연결됐어요. 이제 문서 읽기를 맡아요."}
    try:   # 대화 길(도구 없는 글 답) 확인 — 통과해야 기본 AI(별)를 받을 수 있다
        provider.chat([{"role": "user", "content": TEST_PROMPT}])
    except OcrError as exc:
        return {"ok": True, "chat": False, "message": "문서 읽기는 연결됐지만 대화 확인에 실패했어요: " + _short(str(exc))}
    return {"ok": True, "chat": True, "message": "연결됐어요. 문서 읽기를 맡고, ☆ 기본으로 를 누르면 대화도 맡아요."}
