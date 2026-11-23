"""봉인 안 된 룬은 연습 실행만. Phase 12 의 첫 자물쇠.

약속이 "누구나 룬을 만든다" 에서 **"누구나 말로 요청하고, 봉인된 것만
운영에 들어간다"** 로 바뀌었다. 말로 만든 초안이 곧바로 병원 ERP 에 입고를
넣으면 그 약속은 이름만 남는다.

**화면이 아니라 러너에서 막는다.** 버튼만 잠그면 버튼을 안 거치는 길이
남고, Phase 13 의 원격 명령이 정확히 그 길이다. 그래서 여기 테스트는 화면을
한 번도 안 띄우고 `submit()` 을 직접 부른다.
"""
import pytest

from vader.core import config, seal
from vader.core.runner import JobRunner
from vader.sdk import RuneManifest, RuneResult

from tests.test_runner import FakeStore


class _Quiet:
    manifest = RuneManifest(id="draft_rune", name="말로 만든 초안")

    def run(self, ctx, inputs):
        return RuneResult(ok=True)


@pytest.fixture
def installed(isolated_settings, tmp_path, monkeypatch):
    """설치된 것처럼 소스 폴더를 만든다. 봉인은 아직 없다."""
    root = tmp_path / "runes"
    source = root / "draft_rune"
    source.mkdir(parents=True)
    (source / "rune.py").write_text("# 초안\n", encoding="utf-8")
    seal.trust("draft_rune", source)   # 사람이 설치를 눌렀다
    monkeypatch.setattr(config, "rune_dir", lambda: root)
    return source


@pytest.fixture
def runner(tmp_path):
    return JobRunner(checkpoints=FakeStore(), data_dir=tmp_path / "jobs")


def test_an_unsealed_rune_cannot_run_for_real(runner, installed):
    """초안이 그대로 운영에 들어가면 봉인 원칙이 이름만 남는다."""
    with pytest.raises(RuntimeError, match="승인"):
        runner.submit(_Quiet(), [], params={}, dry_run=False)


def test_an_unsealed_rune_can_still_be_practised(runner, installed):
    """잠그기만 하면 만든 것을 시험할 길이 없다. 연습은 열려 있어야 한다."""
    job = runner.submit(_Quiet(), [], params={}, dry_run=True)

    assert runner.join(job.id, timeout=5)


def test_a_rune_changed_after_install_cannot_even_be_practised(runner, installed):
    """연습도 이 PC 에서 코드를 돌린다. 사람이 확인한 지문이 아니면 돌지 않는다 (2026-10-14 합의)."""
    (installed / "rune.py").write_text("# 설치 뒤 몰래 고침\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="다시 설치"):
        runner.submit(_Quiet(), [], params={}, dry_run=True)


def test_a_sealed_rune_runs_for_real(runner, installed):
    seal.seal("draft_rune", installed, by="마스터", note="골든 통과")

    job = runner.submit(_Quiet(), [], params={}, dry_run=False)

    assert runner.join(job.id, timeout=5)


def test_editing_the_rune_breaks_the_seal(runner, installed):
    """봉인은 **그때 그 소스**에 대한 것이다. 한 글자만 고쳐도 무효다."""
    seal.seal("draft_rune", installed, by="마스터", note="골든 통과")
    (installed / "rune.py").write_text("# 몰래 고침\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="바뀌었습니다"):
        runner.submit(_Quiet(), [], params={}, dry_run=False)


def test_adding_a_file_breaks_the_seal(runner, installed):
    """파일 하나를 더해도 도는 코드가 달라진다."""
    seal.seal("draft_rune", installed, by="마스터", note="골든 통과")
    (installed / "sneaky.py").write_text("# 새 파일\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="바뀌었습니다"):
        runner.submit(_Quiet(), [], params={}, dry_run=False)


def test_deleting_the_ledger_does_not_unlock(runner, installed):
    """원장을 지우는 것이 봉인 우회가 되면 자물쇠가 아니다."""
    seal.seal("draft_rune", installed, by="마스터", note="골든 통과")
    seal._path().unlink()

    with pytest.raises(RuntimeError, match="승인"):
        runner.submit(_Quiet(), [], params={}, dry_run=False)


def test_a_broken_ledger_does_not_unlock(runner, installed):
    """읽기 실패는 원장·일지와 **방향이 반대다.** 저쪽은 계속하고 여기는 잠근다."""
    seal.seal("draft_rune", installed, by="마스터", note="골든 통과")
    seal._path().write_text("{망가진", encoding="utf-8")

    with pytest.raises(RuntimeError, match="승인"):
        runner.submit(_Quiet(), [], params={}, dry_run=False)


def test_another_runes_seal_does_not_count(runner, installed):
    """id 를 안 보면 봉인 하나로 전부 열린다."""
    seal.seal("다른룬", installed, by="마스터", note="")

    with pytest.raises(RuntimeError, match="승인"):
        runner.submit(_Quiet(), [], params={}, dry_run=False)


def test_the_refusal_tells_the_user_what_to_do(runner, installed):
    """그냥 막히면 사용자는 고장으로 안다."""
    with pytest.raises(RuntimeError) as caught:
        runner.submit(_Quiet(), [], params={}, dry_run=False)

    assert "연습 실행" in str(caught.value)


def test_never_sealed_and_seal_broken_are_different_words(runner, installed):
    """"봉인이 없다" 와 "누가 고쳤다" 는 무게가 다르다."""
    never = seal.why("draft_rune", installed)
    seal.seal("draft_rune", installed, by="마스터", note="")
    (installed / "rune.py").write_text("# 고침\n", encoding="utf-8")
    broken = seal.why("draft_rune", installed)

    assert never != broken
    assert "바뀌" in broken


def test_a_rune_that_was_never_installed_is_not_gated(runner, isolated_settings, tmp_path, monkeypatch):
    """**이 자물쇠가 못 막는 것.** 설치 폴더가 없으면 봉인할 대상도 없다.

    배포되는 앱에는 그런 길이 없다 — 룬은 설치를 거쳐야 목록에 뜬다.
    테스트가 쓰는 길이고, 그래서 여기 적어 둔다.
    """
    monkeypatch.setattr(config, "rune_dir", lambda: tmp_path / "없는폴더")

    job = runner.submit(_Quiet(), [], params={}, dry_run=False)

    assert runner.join(job.id, timeout=5)


# --- 화면이 이유를 말한다 --------------------------------------------------------


def test_the_screen_says_why_it_is_locked(installed):
    """그냥 안 눌리면 사용자는 고장으로 안다."""
    from vader.core.view import seal_state

    state = seal_state("draft_rune")

    assert state.locked is True
    assert "승인" in state.reason


def test_a_sealed_rune_leaves_the_screen_alone(installed):
    from vader.core.view import seal_state

    seal.seal("draft_rune", installed, by="마스터", note="")

    assert seal_state("draft_rune") == __import__(
        "vader.core.view", fromlist=["SealState"]
    ).SealState(locked=False)


def test_the_screen_does_not_lock_what_the_runner_would_allow(
    isolated_settings, tmp_path, monkeypatch
):
    """화면과 러너가 다른 규칙을 쓰면 둘 중 하나가 거짓말이 된다."""
    from vader.core.view import seal_state

    monkeypatch.setattr(config, "rune_dir", lambda: tmp_path / "없는폴더")

    assert seal_state("draft_rune").locked is False


def test_installing_does_not_seal(isolated_settings, tmp_path, monkeypatch):
    """**설치는 봉인이 아니다.**

    설치가 봉인을 만들면 자물쇠가 없는 것과 같다 — 초안도 설치는 되기
    때문이다. 봉인은 골든을 통과해야 찍힌다 (다음 항목).
    """
    from vader.core.installer import install

    from tests.test_installer import GOOD

    root = tmp_path / "runes"
    package = tmp_path / "src" / "probe_rune"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text(
        GOOD.format(rid="probe_rune", name="테스트"), encoding="utf-8"
    )
    monkeypatch.setattr(config, "rune_dir", lambda: root)

    installed_at = install(package, root).path

    assert seal.sealed("probe_rune", installed_at) is False
