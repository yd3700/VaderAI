"""진단 기록 (2026-11-28) — 이 PC 에만, 업무 내용·경로·숫자 덩어리는 남기지 않는다."""
from vader.core import config, diag


def test_ui_events_keep_only_fixed_codes_script_positions_and_error_names(isolated_settings, monkeypatch):
    monkeypatch.setattr(config, "HOME", isolated_settings.parent / "home")
    monkeypatch.setattr(diag, "_ui_count", 0)
    assert diag.ui("render", "at render (http://tauri.localhost/app.js:120:5)", "TypeError")
    assert diag.ui("error", "C:/Users/TEST-USER/명세서.js:9:9", "홍길동")
    assert not diag.ui("anything", "core")
    text = (diag.root() / "ui_events.txt").read_text(encoding="utf-8")
    assert "render app.js:120:5 TypeError" in text
    assert "TEST-USER" not in text and "명세서" not in text and "홍길동" not in text and "tauri" not in text
    for _ in range(60):
        diag.ui("drop", "core")
    assert (diag.root() / "ui_events.txt").read_text(encoding="utf-8").count("\n") == 50


def test_start_writes_an_environment_file_and_arms_the_native_crash_log(isolated_settings, monkeypatch):
    import faulthandler
    monkeypatch.setattr(config, "HOME", isolated_settings.parent / "home")
    diag.start()
    try:
        env = next(diag.root().glob("env_*.txt")).read_text(encoding="utf-8")
        assert "vader_version" in env and "is_admin" in env and "uac" in env and "client_animation" in env
        assert all(v.strip() in {"True", "False", "none", "other", "not_found"} or v.strip().replace(".", "").replace(" ", "").replace("bit", "").replace("=", "").isalnum()
                   for v in (line.split(":", 1)[1] for line in env.splitlines()[2:]))
        assert str(isolated_settings.parent) not in env, "경로는 쓰지 않는다"
        assert faulthandler.is_enabled() and list(diag.raw_root().glob("native_raw_*.txt"))
    finally:
        faulthandler.disable()
        diag._native and diag._native.close()


def test_a_native_crash_dump_is_reduced_to_file_names_lines_and_functions():
    raw = r"""Windows fatal exception: access violation 0xc0000005

Current thread 0x00001a2b (most recent call first):
  File "C:\Users\TEST-USER\AppData\Local\VaderAI\runes\eonm_ipgo\driver.py", line 186 in visit
  File "C:\Users\TEST-USER\x\driver.py", line 208 in find_windows
홍길동 명세서.pdf
"""
    out = diag.clean_native(raw)
    assert 'File "driver.py", line 186 in visit' in out and "access violation" in out
    assert "TEST-USER" not in out and "홍길동" not in out and "명세서" not in out


def test_start_never_opens_work_settings_and_keeps_raw_dumps_outside_the_shared_folder(isolated_settings, monkeypatch):
    """켜기만 해도 업무 설정을 읽거나 만들지 않는다. 원본 덤프는 crash 밖(crash-raw)에만, 다음 시작에 정리본으로 옮기고 지운다 (codex1 diag 회신 2)."""
    import faulthandler
    home = isolated_settings.parent / "home2"
    monkeypatch.setattr(config, "HOME", home)
    opened = []
    monkeypatch.setattr(config, "get", lambda *a, **k: opened.append(a))
    monkeypatch.setattr(config, "load", lambda *a, **k: opened.append(a) or {})
    try:
        diag.start()
        assert not (home / "settings.json").exists() and opened == [], "업무 설정을 한 번도 열지 않는다"
        assert not list(diag.root().glob("native_raw_*")) and list(diag.raw_root().glob("native_raw_*"))
        faulthandler.disable()
        diag._native.close()
        next(diag.raw_root().glob("native_raw_*")).write_text(
            'Windows fatal exception: access violation\n  File "C:\\Users\\TEST-USER\\d.py", line 3 in f\n', encoding="utf-8")
        diag.start()                                           # 다시 켬
        shared = "".join(p.read_text(encoding="utf-8") for p in diag.root().glob("native_*.txt"))
        assert 'File "d.py", line 3 in f' in shared and "TEST-USER" not in shared
        assert len(list(diag.raw_root().glob("native_raw_*"))) == 1, "옮긴 원본은 지우고 새 원본 하나만"
    finally:
        faulthandler.disable()
        diag._native and diag._native.close()
