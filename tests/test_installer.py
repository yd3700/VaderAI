"""
설치기 테스트.

지키려는 것: 설치가 반쯤 된 상태를 남기지 않는다. 실패하면 이전 설치본이
그대로 있어야 하고, 성공하면 로더가 바로 찾을 수 있어야 한다.
"""
from tests.private import needs
import json
from pathlib import Path

import pytest

from vader.core import config
from vader.core.installer import InstallError, install, uninstall
from vader.core.loader import discover

GOOD = """
from vader.sdk import RuneField, RuneManifest, RuneResult


class R:
    manifest = RuneManifest(
        id="{rid}",
        name="{name}",
        required_fields=[
            RuneField(name="창제목", kind="window"),
            RuneField(name="출력폴더", kind="text"),
        ],
    )

    def run(self, ctx, inputs):
        return RuneResult(ok=True)


rune = R()
"""

BROKEN = "raise RuntimeError('import 중 터진다')\n"


@pytest.fixture
def source(tmp_path):
    def _make(rid="probe_rune", name="테스트", body=None):
        pkg = tmp_path / "src" / rid
        pkg.mkdir(parents=True, exist_ok=True)
        text = body if body is not None else GOOD.format(rid=rid, name=name)
        (pkg / "__init__.py").write_text(text, encoding="utf-8")
        return pkg

    return _make


@pytest.fixture
def rune_dir(tmp_path):
    return tmp_path / "installed"


@pytest.fixture
def settings(tmp_path, monkeypatch):
    """`runes` 블록을 가진 설정 파일을 세운다."""
    path = tmp_path / "settings.json"
    monkeypatch.setattr(config, "SETTINGS_PATH", path)

    def _write(runes: dict):
        path.write_text(json.dumps({"runes": runes}, ensure_ascii=False), encoding="utf-8")
        config.invalidate()

    _write({})
    yield _write
    config.invalidate()


def test_installs_and_the_loader_finds_it(source, rune_dir, settings):
    result = install(source(), rune_dir)

    assert result.path == rune_dir / "probe_rune"
    assert (result.path / "__init__.py").is_file()

    loaded, failures = discover(rune_dir)
    assert failures == []
    assert [item.manifest.id for item in loaded] == ["probe_rune"]


def test_reports_settings_the_rune_still_needs(source, rune_dir, settings):
    result = install(source(), rune_dir)

    assert result.missing_settings == ["창제목", "출력폴더"]


def test_reports_nothing_missing_once_settings_exist(source, rune_dir, settings):
    settings({"probe_rune": {"창제목": "이온엠", "출력폴더": "C:/out"}})

    assert install(source(), rune_dir).missing_settings == []


def test_does_not_write_settings_itself(source, rune_dir, settings, tmp_path):
    """빈 값을 미리 써 넣으면 룬의 필수값 검사가 통과해 버린다."""
    install(source(), rune_dir)

    saved = json.loads((tmp_path / "settings.json").read_text(encoding="utf-8"))
    assert saved["runes"] == {}


def test_reinstall_overwrites(source, rune_dir, settings):
    install(source(name="예전"), rune_dir)
    result = install(source(name="새것"), rune_dir)

    assert result.manifest.name == "새것"
    loaded, _ = discover(rune_dir)
    assert len(loaded) == 1


def test_a_code_change_without_a_version_bump_is_named(source, rune_dir, settings):
    """코드를 고쳤는데 버전이 그대로면 그렇다고 말한다 — 화면 버전이 안 바뀌던 이유."""
    versioned = lambda name: GOOD.format(rid="probe_rune", name=name).replace("name=", 'version="1.0.0", name=', 1)
    install(source(body=versioned("예전")), rune_dir)
    assert install(source(body=versioned("새것")), rune_dir).change == "same_version"
    assert install(source(body=versioned("새것")), rune_dir).change == "reinstall"


def test_a_broken_rune_is_not_left_installed(source, rune_dir, settings):
    with pytest.raises(InstallError):
        install(source(body=BROKEN), rune_dir)

    assert discover(rune_dir) == ([], [])
    assert not (rune_dir / "probe_rune").exists()


def test_a_failed_upgrade_restores_the_previous_version(source, rune_dir, settings):
    """반쯤 설치된 상태로 남기지 않는다. 이전 것이 계속 돌아야 한다."""
    install(source(name="멀쩡한것"), rune_dir)

    with pytest.raises(InstallError):
        install(source(body=BROKEN), rune_dir)

    loaded, failures = discover(rune_dir)
    assert failures == []
    assert loaded[0].manifest.name == "멀쩡한것"


def test_rejects_a_directory_that_is_not_a_package(tmp_path, rune_dir, settings):
    not_a_rune = tmp_path / "src" / "그냥폴더"
    not_a_rune.mkdir(parents=True)
    (not_a_rune / "readme.txt").write_text("x", encoding="utf-8")

    with pytest.raises(InstallError, match="__init__.py"):
        install(not_a_rune, rune_dir)


def test_rejects_a_manifest_id_that_does_not_match_the_folder(source, rune_dir, settings):
    """폴더명과 id가 어긋나면 설정을 `runes.{id}`로 못 찾는다(D9)."""
    pkg = source(rid="폴더이름")
    (pkg / "__init__.py").write_text(
        GOOD.format(rid="다른id", name="x"), encoding="utf-8"
    )

    with pytest.raises(InstallError):
        install(pkg, rune_dir)

    assert not (rune_dir / "폴더이름").exists()


def test_pycache_is_not_copied(source, rune_dir, settings):
    """소스의 캐시를 옮기지 않는다.

    설치 후 `__pycache__` 자체는 생긴다 — 검증하며 import 하기 때문이다.
    확인할 것은 소스에 있던 파일이 딸려왔는가다.
    """
    pkg = source()
    (pkg / "__pycache__").mkdir()
    (pkg / "__pycache__" / "x.pyc").write_bytes(b"\x00")

    result = install(pkg, rune_dir)

    assert not (result.path / "__pycache__" / "x.pyc").exists()


class TestUninstall:
    """탈부착. `PLAN.md` 최종 완료 조건 — 룬을 설치하거나 제거해도 코어를 수정하지 않는다."""

    def test_removes_the_rune_and_the_loader_stops_finding_it(self, source, rune_dir, settings):
        install(source(), rune_dir)

        assert uninstall("probe_rune", rune_dir) is True
        assert discover(rune_dir) == ([], [])
        assert not (rune_dir / "probe_rune").exists()

    def test_returns_false_when_it_was_not_installed(self, source, rune_dir, settings):
        assert uninstall("없는룬", rune_dir) is False

    def test_settings_survive(self, source, rune_dir, settings, tmp_path):
        """다시 설치할 때 손으로 채운 값을 또 채우게 하면 안 된다."""
        settings({"probe_rune": {"창제목": "이온엠", "출력폴더": "C:/out"}})
        install(source(), rune_dir)

        uninstall("probe_rune", rune_dir)

        saved = json.loads((tmp_path / "settings.json").read_text(encoding="utf-8"))
        assert saved["runes"]["probe_rune"] == {"창제목": "이온엠", "출력폴더": "C:/out"}

    def test_sys_modules_is_left_clean(self, source, rune_dir, settings):
        """폴더는 없는데 아직 import 되는 룬이 남으면 안 된다.

        로더가 경로 해시를 모듈명으로 써서 sys.modules 에 등록한다. 제거가 그걸
        비우지 않으면 같은 프로세스에서 재설치할 때 옛 코드가 돈다.
        """
        import sys

        from vader.core.loader import module_name_for

        pkg = source()
        install(pkg, rune_dir)
        name = module_name_for(rune_dir / "probe_rune")
        assert name in sys.modules, "설치·검증이 모듈을 등록했어야 한다"

        uninstall("probe_rune", rune_dir)

        leftover = [n for n in sys.modules if n == name or n.startswith(name + ".")]
        assert leftover == [], f"sys.modules 에 남았다: {leftover}"

    def test_reinstall_after_uninstall_runs_the_new_code(self, source, rune_dir, settings):
        """제거 후 다시 설치하면 새 코드가 돌아야 한다. 캐시된 옛 모듈이 아니라."""
        install(source(name="예전"), rune_dir)
        uninstall("probe_rune", rune_dir)

        again = install(source(name="새것"), rune_dir)

        assert again.manifest.name == "새것"
        loaded, _ = discover(rune_dir)
        assert loaded[0].manifest.name == "새것"

    def test_removing_one_rune_leaves_the_other(self, source, rune_dir, settings):
        install(source(rid="첫번째"), rune_dir)
        install(source(rid="두번째"), rune_dir)

        uninstall("첫번째", rune_dir)

        loaded, failures = discover(rune_dir)
        assert failures == []
        assert [r.manifest.id for r in loaded] == ["두번째"]

    def test_back_to_zero_runes_is_a_valid_state(self, source, rune_dir, settings):
        """룬 0개가 정상 상태라는 전제로 돌아와야 한다."""
        install(source(), rune_dir)
        uninstall("probe_rune", rune_dir)

        loaded, failures = discover(rune_dir)
        assert loaded == [] and failures == []


@needs("rune-packages/eonm_ipgo")
def test_reinstalling_a_rune_does_not_erase_what_it_learned(tmp_path):
    """룬 코드와 룬이 만든 데이터는 다른 폴더에 살아야 한다.

    `install` 은 `rune_dir()/{id}` 를 통째로 rename 해 백업하고 소스에서 새로
    copytree 한 뒤 백업을 지운다. datadir 이 같은 폴더면 **재설치 한 번에 학습이
    사라진다** — EONM 이면 사람이 하나씩 교정해 쌓은 품목 매핑이 통째로 날아간다.
    """
    pytest.importorskip(
        "pywinauto",
        reason="eonm_ipgo 는 pywinauto 선택 의존성이 필요하다. pip install .[eonm]",
    )

    from vader.core.runner import JobRunner

    runner = JobRunner(checkpoints=None, data_dir=tmp_path / "data")
    datadir = runner._make_datadir("eonm_ipgo")
    learned = datadir / "product_db.csv"
    learned.write_text("vendor,item_name,mapped_code\n거래처A,학습품목,BA000296\n",
                       encoding="utf-8-sig")

    rune_dir = tmp_path / "data" / "runes"
    source = Path(__file__).resolve().parent.parent / "rune-packages" / "eonm_ipgo"
    install(source, rune_dir)
    install(source, rune_dir)   # 재설치

    assert learned.exists(), "재설치가 학습 데이터를 지웠다"
    assert "학습품목" in learned.read_text(encoding="utf-8-sig")
    assert not str(datadir).startswith(str(rune_dir)), (
        f"datadir 이 설치 폴더 안에 있다: {datadir}"
    )


def test_bundled_runes_install_when_missing_or_newer_only(source, rune_dir, settings, monkeypatch):
    """설치본에 실린 룬: 없으면 깔고, 실린 쪽이 새 버전일 때만 올린다 (2026-10-21)."""
    from vader.core.installer import install_bundled

    monkeypatch.setattr(config, "rune_dir", lambda: rune_dir)
    versioned = lambda v, name: GOOD.format(rid="probe_rune", name=name).replace("name=", f'version="{v}", name=', 1)
    bundle = source(body=versioned("1.0.0", "실린것")).parent

    assert install_bundled(bundle) == ["probe_rune"]
    assert install_bundled(bundle) == []                       # 같은 버전 — 안 건드린다
    source(body=versioned("1.1.0", "새것"))
    assert install_bundled(bundle) == ["probe_rune"]
    assert install_bundled(rune_dir / "없는폴더") == []


def test_an_update_without_recordings_keeps_the_installed_ones(source, rune_dir, settings):
    """새 소스에 녹화가 없으면 깔려 있던 녹화를 지우지 않는다. 새 소스에 있으면 그것을 쓴다 (2026-10-22)."""
    install(source(name="예전"), rune_dir)
    (rune_dir / "probe_rune" / "recordings").mkdir()
    (rune_dir / "probe_rune" / "recordings" / "navigation.json").write_text("사람이 한 녹화", encoding="utf-8")

    install(source(name="새것"), rune_dir)
    assert (rune_dir / "probe_rune" / "recordings" / "navigation.json").read_text(encoding="utf-8") == "사람이 한 녹화"
    loaded, _ = discover(rune_dir)
    assert len(loaded) == 1                                     # 승인 지문에 녹화까지 들어가 그대로 불러진다

    shipped = source(name="녹화 실음")
    (shipped / "recordings").mkdir()
    (shipped / "recordings" / "navigation.json").write_text("룬에 실린 녹화", encoding="utf-8")
    install(shipped, rune_dir)
    assert (rune_dir / "probe_rune" / "recordings" / "navigation.json").read_text(encoding="utf-8") == "룬에 실린 녹화"


def test_one_bundled_rune_failing_does_not_stop_the_rest(source, rune_dir, settings, monkeypatch):
    """실린 룬 하나가 실패해도 나머지는 깔리고, 실패는 화면이 읽을 수 있게 남는다 (2026-10-27)."""
    from vader.core import installer

    monkeypatch.setattr(config, "rune_dir", lambda: rune_dir)
    bundle = source().parent
    broken = bundle / "a_broken"
    broken.mkdir()
    (broken / "__init__.py").write_text("raise RuntimeError('깨짐')\n", encoding="utf-8")

    assert installer.install_bundled(bundle) == ["probe_rune"]
    assert [f.rune_id for f in installer.bundle_failures] == ["a_broken"]
