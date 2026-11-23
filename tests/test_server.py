"""폰에서 같은 펫을 만난다. Phase 15.

**들어오는 문이다.** 나가는 문은 "무엇을 보내나" 를 지키고 들어오는 문은
"누구를 들이나" 를 지킨다. 아래 절반이 두 번째 것이다.

`handle()` 을 직접 부르는 테스트와 진짜 소켓을 여는 테스트를 나눴다. 전자가
규칙을 지키고, 후자가 **정말로 붙는지**를 한 번 본다.
"""
import ast
import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from vader.core import conversation, pairing, pet as pet_module, server as server_module

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def server(isolated_settings, monkeypatch):
    conversation.reset()
    pet_module.save(pet_module.Pet(id="p1", name="바다"))
    made = server_module.PetServer(port=0)
    yield made
    made.stop()
    conversation.reset()


@pytest.fixture
def paired(server):
    """붙은 기기 하나. 토큰을 돌려준다."""
    pending = server.show_code()
    token = pairing.claim(pending, pending.code, "내 폰")
    server.pending = None
    return token


# --- 누구를 들이나 --------------------------------------------------------------


def test_without_a_token_there_is_nothing_here(server):
    """401 은 "여기 뭔가 있다" 를 알려 주는 셈이다."""
    status, body = server.handle("GET", "/api/pet", "", {})

    assert status == 404


def test_a_wrong_token_is_also_nothing(server, paired):
    status, _ = server.handle("GET", "/api/pet", "남의 토큰", {})

    assert status == 404


def test_a_paired_device_meets_the_pet(server, paired):
    status, body = server.handle("GET", "/api/pet", paired, {})

    assert status == 200
    assert body["name"] == "바다"
    # 답이 오는 것이 곧 온라인이다. 따로 심장 박동을 만들지 않는다.
    assert body["online"] is True


def test_pairing_needs_a_code_on_screen(server):
    """코드를 안 띄웠으면 아무도 못 붙는다."""
    status, _ = server.handle("POST", "/pair", "", {"code": "000000", "name": "몰래"})

    assert status == 403
    assert pairing.devices() == []


def test_a_used_code_disappears_from_the_screen(server):
    pending = server.show_code()

    server.handle("POST", "/pair", "", {"code": pending.code, "name": "내 폰"})

    assert server.pending is None, "쓴 코드가 화면에 남아 있으면 안 된다"


def test_closing_the_window_kills_the_code(server):
    """창을 닫았는데 코드가 도는 것이 더 이상하다."""
    server.start()
    server.show_code()

    server.stop()

    assert server.pending is None


def test_an_unknown_route_is_nothing(server, paired):
    status, _ = server.handle("GET", "/api/../settings.json", paired, {})

    assert status == 404


# --- 무엇이 나가나 --------------------------------------------------------------


def test_the_server_cannot_see_the_observations():
    """문이 관찰 모듈을 못 본다. `data-map.md` 의 규칙 2."""
    tree = ast.parse((ROOT / "vader/core/server.py").read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)

    assert not any("observe" in n or "character" in n for n in imported)


def test_the_server_cannot_run_a_rune():
    """원격 명령은 Phase 13 이다. 승인·감사·멱등성이 거기 전제로 붙어 있다."""
    source = (ROOT / "vader/core/server.py").read_text(encoding="utf-8")

    assert "runner" not in source
    assert "JobRunner" not in source


def test_the_page_pulls_nothing_from_outside(server):
    """CDN 하나만 있어도 그 순간 이 기계 밖으로 요청이 나간다."""
    _, page = server.handle("GET", "/", "", {})

    for outside in ("http://", "https://", "//cdn", "@import"):
        assert outside not in page


# --- 대화가 이어진다 ------------------------------------------------------------


def test_the_phone_sees_the_desktop_conversation(server, paired, monkeypatch):
    """완료 기준. 폰이 자기 대화를 따로 가지면 그건 다른 펫이다."""
    from vader.core.chat import Message

    conversation.session().messages.extend(
        [Message("user", "데스크톱에서 한 말"), Message("assistant", "네")]
    )

    status, body = server.handle("GET", "/api/history", paired, {})

    assert status == 200
    assert [m["content"] for m in body["messages"]] == ["데스크톱에서 한 말", "네"]


def test_a_word_from_the_phone_lands_in_the_one_conversation(server, paired, monkeypatch):
    monkeypatch.setattr(conversation, "say", lambda text, **k: f"{text} 잘 들었습니다")

    status, body = server.handle("POST", "/api/say", paired, {"text": "폰에서"})

    assert status == 200
    assert body["answer"] == "폰에서 잘 들었습니다"


def test_an_empty_word_is_refused(server, paired):
    status, _ = server.handle("POST", "/api/say", paired, {"text": "   "})

    assert status == 400


def test_a_failure_does_not_send_the_stack_to_the_phone(server, paired, monkeypatch):
    def explode(text, **kwargs):
        raise RuntimeError("api 키가 sk-1234 입니다")

    monkeypatch.setattr(conversation, "say", explode)

    status, body = server.handle("POST", "/api/say", paired, {"text": "안녕"})

    assert status == 502
    assert "sk-1234" not in json.dumps(body, ensure_ascii=False)


def test_the_phone_cannot_bring_its_own_approval(server, paired, monkeypatch):
    """폰이 자기 승인 통로를 가져오면 그게 원격이 스스로 허락하는 길이다."""
    seen = []
    monkeypatch.setattr(
        conversation, "say", lambda text, **k: seen.append(k.get("approve")) or "네"
    )

    server.handle("POST", "/api/say", paired, {"text": "설정 바꿔줘"})

    assert seen == [None], "데스크톱이 안 열려 있으면 승인 통로가 없다"


# --- 진짜 소켓 하나 -------------------------------------------------------------


def test_a_real_phone_can_actually_reach_it(server):
    """`handle()` 만 부르는 테스트는 HTTP 를 한 번도 안 지난다.

    포트·헤더·본문 파싱이 틀려도 위가 전부 통과한다. 그래서 한 번은 진짜로 연다.
    """
    port = server.start()
    pending = server.show_code()

    got = _post(port, "/pair", {"code": pending.code, "name": "진짜 폰"})
    pet = _get(port, "/api/pet", got["token"])

    assert pet["name"] == "바다"


def test_a_real_request_without_a_token_gets_nothing(server):
    port = server.start()

    with pytest.raises(urllib.error.HTTPError) as caught:
        _get(port, "/api/pet", "")

    assert caught.value.code == 404


def test_a_huge_body_is_not_swallowed(server, paired, monkeypatch):
    """상한이 없으면 아무나 메모리를 채울 수 있다.

    **상태 코드를 안 본다.** 서버가 본문을 안 읽고 답하면 클라이언트는 아직
    쓰는 중이고, 그 연결은 깔끔한 400 으로 끝날 수도 있고 리셋으로 끝날 수도
    있다. 어느 쪽인지는 그날 타이밍이 정한다 — 실제로 이 테스트가 그것 때문에
    간헐적으로 깨졌다.

    지키는 것은 하나다. **읽지 않는다.**
    """
    port = server.start()
    reached = []
    monkeypatch.setattr(
        conversation, "say", lambda text, **k: reached.append(text) or "네"
    )

    with pytest.raises((urllib.error.HTTPError, OSError)):
        _post(port, "/api/say", {"text": "가" * server_module.MAX_BODY}, paired)

    assert reached == [], "상한을 넘은 본문은 대화까지 못 간다"


def _get(port: int, route: str, token: str) -> dict:
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{route}",
        headers={"Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


def _post(port: int, route: str, body: dict, token: str = "") -> dict:
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{route}",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))
