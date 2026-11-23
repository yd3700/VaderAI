"""기기 페어링. Phase 15.

여섯 자리는 100만 가지다. 계정 서버 없이 그걸로 문을 여는 이상, **코드의
규칙이 곧 보안이다.**
"""
import time

from vader.core import pairing


def test_a_code_becomes_a_token(isolated_settings):
    pending = pairing.new_code()

    token = pairing.claim(pending, pending.code, "내 폰")

    assert token
    assert pairing.device_for(token).name == "내 폰"


def test_a_wrong_code_gets_nothing(isolated_settings):
    pending = pairing.new_code()
    wrong = f"{(int(pending.code) + 1) % 1_000_000:06d}"

    assert pairing.claim(pending, wrong, "남의 폰") == ""
    assert pairing.devices() == []


def test_a_code_dies_after_five_misses(isolated_settings):
    """유효 시간만으로는 부족하다 — 5분이면 여섯 자리를 다 찔러 볼 수 있다."""
    pending = pairing.new_code()
    wrong = f"{(int(pending.code) + 1) % 1_000_000:06d}"

    for _ in range(pairing.MAX_ATTEMPTS):
        pairing.claim(pending, wrong, "찌르는 놈")

    assert pairing.claim(pending, pending.code, "주인") == "", "죽은 코드는 안 먹는다"


def test_an_expired_code_gets_nothing(isolated_settings, monkeypatch):
    pending = pairing.new_code()
    monkeypatch.setattr(
        pairing.time, "monotonic", lambda: pending.born + pairing.CODE_TTL + 1
    )

    assert pairing.claim(pending, pending.code, "늦은 폰") == ""


def test_a_code_works_once(isolated_settings):
    """한 코드로 두 기기가 붙으면 몇 대가 붙었는지 사용자가 셀 수 없다."""
    pending = pairing.new_code()
    pairing.claim(pending, pending.code, "첫 폰")

    assert pairing.claim(pending, pending.code, "둘째 폰") == ""
    assert [d.name for d in pairing.devices()] == ["첫 폰"]


def test_two_codes_are_not_the_same(isolated_settings):
    codes = {pairing.new_code().code for _ in range(50)}

    assert len(codes) > 40, "예측 가능한 코드는 코드가 아니다"


def test_an_unknown_token_has_no_owner(isolated_settings):
    pairing.claim(pairing.new_code(), pairing.new_code().code, "아무나")

    assert pairing.device_for("아무 토큰이나") is None
    assert pairing.device_for("") is None


def test_a_lost_phone_can_be_cut_off(isolated_settings):
    """잃어버렸을 때 사용자가 할 일이 "기다리기" 여서는 안 된다."""
    pending = pairing.new_code()
    token = pairing.claim(pending, pending.code, "잃어버린 폰")

    assert pairing.revoke(token) is True
    assert pairing.device_for(token) is None
    assert pairing.revoke(token) is False, "두 번 끊을 것은 없다"


def test_the_devices_survive_a_restart(isolated_settings):
    pending = pairing.new_code()
    token = pairing.claim(pending, pending.code, "내 폰")

    assert pairing.device_for(token) is not None, "디스크에서 다시 읽는다"


def test_a_broken_device_file_is_an_empty_list(isolated_settings):
    pairing._path().parent.mkdir(parents=True, exist_ok=True)
    pairing._path().write_text("{그냥 쓰레기", encoding="utf-8")

    assert pairing.devices() == []


def test_the_code_is_not_written_to_disk(isolated_settings):
    """앱이 죽어도 코드가 살아남으면 안 된다. 화면에 떠 있는 동안만이다."""
    pending = pairing.new_code()

    found = [
        p
        for p in isolated_settings.parent.rglob("*")
        if p.is_file() and pending.code in p.read_text(encoding="utf-8", errors="ignore")
    ]

    assert found == []


def test_a_nameless_device_still_gets_a_name(isolated_settings):
    """목록에 빈 줄이 뜨면 무엇을 끊어야 할지 모른다."""
    pending = pairing.new_code()
    token = pairing.claim(pending, pending.code, "   ")

    assert pairing.device_for(token).name == "이름 없는 기기"


def test_time_moves_forward_for_real(isolated_settings):
    """`seconds_left` 가 화면에 그대로 뜬다. 거꾸로 가면 안 된다."""
    pending = pairing.new_code()
    first = pending.seconds_left
    time.sleep(0.01)

    assert 0 < pending.seconds_left <= first


def test_a_korean_token_does_not_blow_up(isolated_settings):
    """`compare_digest` 는 비 ASCII 문자열에 TypeError 를 던진다.

    비교 대상은 폰이 보낸 값이라 무엇이든 온다. 여기서 예외가 나면 "틀린
    토큰" 이 404 가 아니라 **500** 이 된다 — 없는 것처럼 보여야 할 자리에서
    서버가 비명을 지르는 셈이다.
    """
    pending = pairing.new_code()
    pairing.claim(pending, pending.code, "내 폰")

    assert pairing.device_for("한글토큰") is None
    assert pairing.claim(pairing.new_code(), "한글코드", "누구") == ""
    assert pairing.revoke("한글토큰") is False


def test_the_comparison_stays_constant_time():
    """타이밍은 검사로 못 잡는다. **그래서 코드를 본다.**

    `==` 로 바꿔도 모든 동작 테스트가 통과한다 — 돌연변이 검사에서 실제로
    안 잡혔다. 무너져도 아무도 모르는 성질이라 여기서만 지킨다.
    """
    from pathlib import Path

    source = (Path(__file__).resolve().parents[1] / "vader/core/pairing.py").read_text(
        encoding="utf-8"
    )

    assert "secrets.compare_digest" in source
    assert "def _same" in source, "비교는 한 곳을 지난다"
