"""설치된 룬 고치기 — [수정] 은 제작소로, 고칠 곳은 하나, 녹화는 룬 안 (2026-10-15 마스터 결정, codex1 합의).

지키려는 것: 설치본을 직접 고치지 않는다 · 고치는 중인 편집본을 덮지 않는다 · 검사·설치가 연결한 원본을 쓴다 ·
룬 안 녹화가 깨졌으면 일부만 누르거나 옛 녹화로 넘어가지 않는다 · 녹화가 바뀌면 지문(승인)이 바뀐다.
"""
import json

import pytest

from vader.core import config, recorder, seal, workshop

MANIFEST = 'from vader.sdk import RuneManifest\nMANIFEST = RuneManifest(id="{id}", name="입고", version="{v}")\n'


def make_rune(folder, rune_id="eonm_ipgo", version="1.0.0"):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "__init__.py").write_text(MANIFEST.format(id=rune_id, v=version), encoding="utf-8")
    return folder


def recording(steps):
    return json.dumps({"format": "vader-recording/1", "steps": steps}, ensure_ascii=False)


@pytest.fixture
def installed():
    return make_rune(config.rune_dir() / "eonm_ipgo")


def project_of(rune_id="eonm_ipgo"):
    return next(p for p in workshop.projects() if p["id"] == rune_id)


def test_edit_copies_the_installed_rune_once_and_never_overwrites(installed):
    workshop.edit("eonm_ipgo")
    copy = workshop.package_dir("eonm_ipgo")
    assert copy == workshop.project("eonm_ipgo") / "eonm_ipgo" and (copy / "__init__.py").is_file()
    assert project_of()["installed"] == "same"

    (copy / "new.py").write_text("x = 1\n", encoding="utf-8")
    workshop.edit("eonm_ipgo")                                  # 다시 눌러도 고치던 것은 그대로
    assert (copy / "new.py").exists() and project_of()["installed"] == "changed"


def test_editing_needs_an_installed_rune():
    with pytest.raises(workshop.WorkshopError):
        workshop.edit("eonm_ipgo")


def test_a_linked_source_is_the_one_place_checked_and_installed(installed, tmp_path):
    workshop.edit("eonm_ipgo")
    origin = make_rune(tmp_path / "repo" / "eonm_ipgo", version="1.1.0")
    workshop.link_source("eonm_ipgo", origin)
    assert workshop.package_dir("eonm_ipgo") == origin.resolve()
    assert workshop.validate_static("eonm_ipgo")["fingerprint"] == seal.fingerprint(origin)
    assert project_of()["version"] == "1.1.0" and project_of()["source"] == str(origin.resolve())


def test_linking_refuses_another_rune_and_the_installed_folder(installed, tmp_path):
    with pytest.raises(workshop.WorkshopError, match="룬 폴더가 아닙니다"):
        workshop.link_source("eonm_ipgo", make_rune(tmp_path / "other", rune_id="supply_weekly"))
    with pytest.raises(workshop.WorkshopError, match="직접 고치지"):
        workshop.link_source("eonm_ipgo", installed)


def test_the_old_recording_comes_into_the_rune_once_and_changes_its_fingerprint(installed):
    recorder.path_for("navigation").parent.mkdir(parents=True, exist_ok=True)
    recorder.path_for("navigation").write_text(recording([{"at": "x", "keys": "{TAB}"}]), encoding="utf-8")
    workshop.edit("eonm_ipgo")
    before = seal.fingerprint(workshop.package_dir("eonm_ipgo"))
    assert project_of()["legacy_recordings"] == ["navigation"]

    workshop.import_recording("eonm_ipgo", "navigation")
    assert project_of()["recordings"] == ["navigation"] and project_of()["legacy_recordings"] == []
    assert recorder.path_for("navigation").exists()               # 원본은 지우지 않는다
    assert seal.fingerprint(workshop.package_dir("eonm_ipgo")) != before   # 녹화도 승인 대상
    with pytest.raises(workshop.WorkshopError):
        workshop.import_recording("eonm_ipgo", "navigation")      # 덮지 않는다
    with pytest.raises(workshop.WorkshopError):
        workshop.import_recording("ins_singo", "navigation")      # 주인이 아닌 룬에는 안 준다


def test_a_rune_recording_is_read_strictly(tmp_path):
    rune = tmp_path / "eonm_ipgo"
    (rune / "recordings").mkdir(parents=True)
    (rune / "recordings" / "navigation.json").write_text(
        recording([{"at": "x", "keys": "{TAB}"}, {"at": "y", "keys": "{ENTER}"}]), encoding="utf-8")
    assert [s.keys for s in recorder.load("navigation", rune)] == ["{TAB}", "{ENTER}"]

    (rune / "recordings" / "navigation.json").write_text(
        recording([{"at": "x", "keys": "{TAB}"}, "깨진 줄"]), encoding="utf-8")
    assert recorder.load("navigation", rune) == []                # 일부만 누르지 않는다
    assert recorder.load("../navigation", rune) == []             # 룬 밖으로 못 나간다


def test_the_editing_rune_is_named_in_the_guide_and_the_first_message(installed, monkeypatch):
    from vader.core import clients

    workshop.edit("eonm_ipgo")
    assert str(workshop.package_dir("eonm_ipgo")) in workshop.guide()
    monkeypatch.setattr(clients, "_exe", lambda client: r"C:\tools\claude.exe")
    monkeypatch.setattr(clients, "_registered", lambda client: True)
    assert "eonm_ipgo 를 고치자" in clients.console_command("claude", "eonm_ipgo")[1]
    assert clients.console_command("claude")[1] == f'"{clients.FIRST_MESSAGE}"'
