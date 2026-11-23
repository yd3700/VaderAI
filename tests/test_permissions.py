"""권한·신뢰 모델 테스트 (Phase 7).

이 모델은 샌드박스가 아니다. 룬은 인프로세스 임의 파이썬이라 런타임 강제가
불가능하다. 막는 것은 **선언하지 않은 능력을 조용히 쓰는 것** 하나다.

마지막 클래스가 못 막는 것을 명시한다. 못 막는 것을 테스트로 적어 두는 이유는
나중에 이 모델을 샌드박스로 착각하지 않게 하기 위해서다.
"""
from __future__ import annotations
from tests.private import needs

from pathlib import Path

import pytest

from vader.core.installer import InstallError, _declared, install
from vader.core.permissions import describe, required_capabilities, undeclared

ROOT = Path(__file__).resolve().parent.parent


def make_rune(tmp_path: Path, body: str, *, permissions: str = "[]") -> Path:
    source = tmp_path / "probe_rune"
    source.mkdir(parents=True, exist_ok=True)
    (source / "__init__.py").write_text("from .rune import rune\n", encoding="utf-8")
    (source / "rune.py").write_text(
        "from vader.sdk import RuneManifest, RuneResult\n"
        f"{body}\n"
        "\n"
        "class Probe:\n"
        "    manifest = RuneManifest(\n"
        '        id="probe_rune", name="검증용",\n'
        f"        permissions={permissions},\n"
        "    )\n"
        "\n"
        "    def run(self, ctx, inputs):\n"
        "        return RuneResult(ok=True)\n"
        "\n"
        "rune = Probe()\n",
        encoding="utf-8",
    )
    return source


# ── 스캐너 ─────────────────────────────────────────────────────

class TestScanner:

    def test_a_rune_using_only_the_sdk_needs_nothing(self, tmp_path):
        source = make_rune(tmp_path, "")

        assert required_capabilities(source) == {}
        assert undeclared([], source) == {}

    @pytest.mark.parametrize(
        "body,capability",
        [
            ("import pywinauto", "ui.automation"),
            ("import ctypes", "native.code"),
            ("import socket", "network"),
            ("from urllib import request", "network"),
            ("import subprocess", "process.spawn"),
            ("import importlib", "dynamic.code"),
        ],
    )
    def test_each_module_maps_to_its_capability(self, tmp_path, body, capability):
        source = make_rune(tmp_path, body)

        assert capability in required_capabilities(source)

    def test_reasons_name_the_file_and_the_module(self, tmp_path):
        """거부 메시지가 무엇을 고쳐야 하는지 말해야 한다."""
        source = make_rune(tmp_path, "import pywinauto")

        reasons = required_capabilities(source)["ui.automation"]
        assert reasons == ["rune.py: pywinauto"]

    def test_eval_is_caught_without_any_import(self, tmp_path):
        """동적 코드가 가장 싼 우회로다. import 가 없어도 잡는다."""
        source = make_rune(tmp_path, 'X = eval("1 + 1")')

        assert "dynamic.code" in required_capabilities(source)

    def test_a_declared_capability_is_not_reported(self, tmp_path):
        source = make_rune(tmp_path, "import socket", permissions='["network"]')

        assert undeclared(["network"], source) == {}

    def test_an_unparseable_file_does_not_crash_the_scan(self, tmp_path):
        source = make_rune(tmp_path, "import socket")
        (source / "broken.py").write_text("def ( 깨진 문법", encoding="utf-8")

        # 파싱 실패는 뒤의 _verify 가 막는다. 스캐너가 먼저 터지면 안 된다.
        assert "network" in required_capabilities(source)


# ── 설치 거부 ──────────────────────────────────────────────────

class TestInstallRefuses:

    def test_an_undeclared_capability_blocks_installation(self, tmp_path):
        source = make_rune(tmp_path, "import socket")
        rune_dir = tmp_path / "runes"

        with pytest.raises(InstallError, match="선언하지 않은 권한"):
            install(source, rune_dir)

    def test_the_message_names_the_capability_and_the_file(self, tmp_path):
        source = make_rune(tmp_path, "import pywinauto")

        with pytest.raises(InstallError) as caught:
            install(source, tmp_path / "runes")

        message = str(caught.value)
        assert "ui.automation" in message
        assert "rune.py" in message
        assert "permissions" in message

    def test_nothing_reaches_disk_when_refused(self, tmp_path):
        """검사는 복사 **전에** 한다. 거부된 룬은 디스크에 닿지 않는다."""
        source = make_rune(tmp_path, "import subprocess")
        rune_dir = tmp_path / "runes"

        with pytest.raises(InstallError):
            install(source, rune_dir)

        assert not (rune_dir / "probe_rune").exists()

    def test_declaring_it_allows_installation(self, tmp_path):
        source = make_rune(tmp_path, "import socket", permissions='["network"]')

        result = install(source, tmp_path / "runes")

        assert result.manifest.id == "probe_rune"
        assert result.granted == ["네트워크에 접속합니다"]

    def test_manifest_permissions_are_read_without_importing(self, tmp_path):
        """검사 전에 룬 코드를 실행하면 검사의 의미가 없다.

        import 대신 AST 로 읽는다. import 하면 모듈 최상단이 즉시 돈다.
        """
        source = make_rune(tmp_path, "import socket", permissions='["network"]')
        (source / "rune.py").write_text(
            (source / "rune.py").read_text(encoding="utf-8").replace(
                "import socket", "import socket\nraise RuntimeError('import 되면 안 된다')"
            ),
            encoding="utf-8",
        )

        assert _declared(source) == ["network"]


# ── 실제 룬 둘 ─────────────────────────────────────────────────

@needs("rune-packages/eonm_ipgo")
class TestRealRunes:

    def test_ins_singo_needs_no_capability(self):
        """SDK 만 쓰는 룬은 권한이 필요 없다. 그게 기본이어야 한다."""
        source = ROOT / "rune-packages" / "ins_singo"

        assert required_capabilities(source) == {}
        assert _declared(source) == []

    def test_eonm_declares_what_its_driver_actually_uses(self):
        """검사를 켜자 `native.code` 누락이 드러났다 — ctypes 를 쓰는데 없었다.

        2026-08-11 에 `process.spawn` 이 늘었다. 이온엠을 직접 띄우기 때문이고,
        설치 화면이 사용자에게 "다른 프로그램을 실행합니다" 라고 말한다.
        **이 목록이 늘어날 때 사람이 한 번 보라고 여기 적어 둔다.**
        """
        source = ROOT / "rune-packages" / "eonm_ipgo"

        declared = _declared(source)
        assert set(declared) == {"ui.automation", "native.code", "process.spawn"}
        assert undeclared(declared, source) == {}

    def test_every_installed_rune_declares_everything_it_uses(self):
        """새 룬을 넣을 때 이 테스트가 잡는다."""
        offenders = {}
        for source in sorted((ROOT / "rune-packages").iterdir()):
            if not source.is_dir() or source.name == "__pycache__":
                continue
            missing = undeclared(_declared(source), source)
            if missing:
                offenders[source.name] = sorted(missing)
        assert offenders == {}


# ── 못 막는 것 ─────────────────────────────────────────────────

class TestWhatThisDoesNotStop:
    """샌드박스가 아니다. 한계를 테스트로 적어 둔다.

    나중에 누가 이 모델을 강제 격리로 착각하고 외부 룬을 열면 안 된다.
    """

    def test_a_hidden_import_slips_through(self, tmp_path):
        """`__import__("sock" + "et")` 은 정적으로 모듈 이름이 보이지 않는다.

        호출 이름이 `__import__` 라서 `dynamic.code` 로는 잡힌다 — 그게 이
        스캐너가 동적 import 를 능력으로 보는 이유다. 하지만 `network` 는
        선언하지 않고 지나간다.
        """
        source = make_rune(tmp_path, 'M = __import__("sock" + "et")')

        found = required_capabilities(source)
        assert "dynamic.code" in found      # 우회 시도 자체는 보인다
        assert "network" not in found       # 무엇을 쓰는지는 안 보인다

    def test_a_rune_that_declares_dynamic_code_can_do_anything(self, tmp_path):
        """선언만 하면 통과한다. 신뢰 판단은 설치하는 사람에게 남는다."""
        source = make_rune(
            tmp_path, 'M = __import__("socket")', permissions='["dynamic.code"]'
        )

        assert undeclared(["dynamic.code"], source) == {}
        assert install(source, tmp_path / "runes").manifest.id == "probe_rune"

    def test_the_module_says_plainly_that_it_is_not_a_sandbox(self):
        """문서가 능력을 과장하면 그게 가장 위험하다."""
        source = (ROOT / "vader" / "core" / "permissions.py").read_text(encoding="utf-8")

        assert "샌드박스가 아니다" in source
        assert "못 막는 것" in source

    def test_describe_does_not_hide_an_unknown_capability(self):
        """모르는 권한을 조용히 넘기면 사용자가 빈 목록을 보고 안전하다고 믿는다."""
        assert describe(["뭔가.새로운것"]) == ["알 수 없는 권한: 뭔가.새로운것"]


# ── 버전 호환 (Phase 7) ────────────────────────────────────────

class TestVersionCompatibility:
    """설치·업데이트·제거와 버전 호환 정책."""

    def make_versioned(self, tmp_path: Path, version: str) -> Path:
        source = make_rune(tmp_path / version, "")
        text = (source / "rune.py").read_text(encoding="utf-8")
        (source / "rune.py").write_text(
            text.replace('name="검증용",', f'name="검증용", version="{version}",'),
            encoding="utf-8",
        )
        return source

    def test_a_fresh_install_is_new(self, tmp_path):
        result = install(self.make_versioned(tmp_path, "1.0.0"), tmp_path / "runes")

        assert result.change == "new"
        assert result.previous_version == ""

    def test_the_same_version_again_is_a_reinstall(self, tmp_path):
        rune_dir = tmp_path / "runes"
        install(self.make_versioned(tmp_path, "1.0.0"), rune_dir)

        result = install(self.make_versioned(tmp_path, "1.0.0"), rune_dir)

        assert result.change == "reinstall"
        assert result.previous_version == "1.0.0"

    def test_a_higher_version_is_an_upgrade(self, tmp_path):
        rune_dir = tmp_path / "runes"
        install(self.make_versioned(tmp_path, "1.0.0"), rune_dir)

        result = install(self.make_versioned(tmp_path, "1.2.0"), rune_dir)

        assert result.change == "upgrade"
        assert result.previous_version == "1.0.0"

    def test_a_lower_version_is_a_downgrade_and_is_allowed(self, tmp_path):
        """다운그레이드도 사용자의 선택이다. 알려주되 막지 않는다."""
        rune_dir = tmp_path / "runes"
        install(self.make_versioned(tmp_path, "2.0.0"), rune_dir)

        result = install(self.make_versioned(tmp_path, "1.9.9"), rune_dir)

        assert result.change == "downgrade"
        assert result.previous_version == "2.0.0"

    def test_double_digit_segments_compare_numerically(self):
        """문자열 비교면 "1.10.0" < "1.9.0" 이 된다."""
        from vader.core.installer import _classify

        assert _classify("1.9.0", "1.10.0") == "upgrade"
        assert _classify("1.10.0", "1.9.0") == "downgrade"


class TestCheckpointIgnoresAnUpgradedRune:
    """payload 의 모양은 룬이 정한다. 룬이 바뀌면 옛 payload 를 먹이면 안 된다."""

    def store(self, tmp_path):
        from vader.core.checkpoint import CheckpointStore

        return CheckpointStore(tmp_path / "checkpoints")

    def key(self, tmp_path):
        from vader.sdk import CheckpointKey, Input

        source = tmp_path / "a.pdf"
        source.write_bytes(b"payload")
        return CheckpointKey.from_inputs("probe", [Input(path=source)])

    def test_the_same_version_resumes(self, tmp_path):
        store, key = self.store(tmp_path), self.key(tmp_path)
        store.save(key, {"단계": 2}, rune_version="1.0.0")

        assert store.load(key, rune_version="1.0.0") == {"단계": 2}

    def test_a_different_version_starts_over(self, tmp_path):
        store, key = self.store(tmp_path), self.key(tmp_path)
        store.save(key, {"단계": 2}, rune_version="1.0.0")

        # 처음부터 실행한다. 예외가 아니라 None 이다 — 재개 실패가 Job 을 죽이면 안 된다.
        assert store.load(key, rune_version="2.0.0") is None

    def test_an_old_file_without_the_field_still_resumes(self, tmp_path):
        """없던 필드가 생긴 것으로 기존 재개를 깨뜨리지 않는다."""
        import json

        store, key = self.store(tmp_path), self.key(tmp_path)
        store.save(key, {"단계": 2}, rune_version="1.0.0")
        path = store._path(key)
        document = json.loads(path.read_text(encoding="utf-8"))
        del document["rune_version"]
        path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")

        assert store.load(key, rune_version="9.9.9") == {"단계": 2}

    def test_not_asking_about_the_version_still_works(self, tmp_path):
        store, key = self.store(tmp_path), self.key(tmp_path)
        store.save(key, {"단계": 2}, rune_version="1.0.0")

        assert store.load(key) == {"단계": 2}

    def test_the_store_satisfies_the_protocol(self, tmp_path):
        """대역이 인터페이스에 뒤처지는 일이 세 번 있었다. 준수를 검사한다."""
        from vader.core.context import CheckpointStore as Protocol

        assert isinstance(self.store(tmp_path), Protocol)


def test_url_string_helpers_are_not_network():
    """`urllib.parse` 는 소켓을 안 연다. URL 문자열을 만들고 쪼갤 뿐이다.

    이걸 network 로 세면 주소를 조립하기만 하는 룬이 네트워크 권한을
    선언하게 되고, **선언이 실제 능력을 안 나타내면 선언 자체가 값을 잃는다.**
    실제로 걸렸다 — 날씨 룬은 `ctx.http_get` 으로만 나가는데 `quote()` 하나
    때문에 네트워크 권한을 요구받았다.
    """
    import tempfile
    from pathlib import Path

    from vader.core import permissions

    root = Path(tempfile.mkdtemp())
    (root / "a.py").write_text("from urllib.parse import quote\n", encoding="utf-8")
    assert permissions.required_capabilities(root) == {}

    (root / "b.py").write_text("import urllib.request\n", encoding="utf-8")
    assert "network" in permissions.required_capabilities(root)
