"""자동 업데이트 — 피드 주소 고르기와 화면이 읽는 상태."""
import sys
import types

from vader.core import config, update


def test_feed_prefers_settings_then_bundled_file(monkeypatch, tmp_path):
    bundled = tmp_path / "update_feed.txt"
    monkeypatch.setattr(config, "TEMPLATE_PATH", tmp_path / "settings.template.json")
    monkeypatch.setattr(config, "device_get", lambda key, default=None: None)
    assert update.feed() is None                      # 둘 다 없으면 업데이트를 하지 않는다
    bundled.write_text("https://example.test/api/updates/k/\n", encoding="utf-8")
    assert update.feed() == "https://example.test/api/updates/k/"
    monkeypatch.setattr(config, "device_get", lambda key, default=None: r"C:\feed" if key == "update.feed" else None)
    assert update.feed() == r"C:\feed"                # 이 PC 의 기기 설정이 설치본 주소보다 먼저


def _fake_velopack(monkeypatch, *, info=None, fail=None):
    class Manager:
        def __init__(self, source):
            self.source = source

        def check_for_updates(self):
            if fail:
                raise RuntimeError(fail)
            return info

        def download_updates(self, found, progress):
            progress(40)
            progress(100)

    monkeypatch.setitem(sys.modules, "velopack", types.SimpleNamespace(UpdateManager=Manager))
    monkeypatch.setattr(update, "_status", {"state": "checking"})
    monkeypatch.setattr(update, "_pending", None)


def test_status_walks_download_to_ready(monkeypatch):
    info = types.SimpleNamespace(TargetFullRelease=types.SimpleNamespace(Version="0.1.9"))
    _fake_velopack(monkeypatch, info=info)
    update._fetch("feed")
    assert update.status() == {"state": "ready", "version": "0.1.9", "progress": 100}
    assert update._pending is not None


def test_status_current_and_failure_never_raise(monkeypatch):
    _fake_velopack(monkeypatch)
    update._fetch("feed")
    assert update.status()["state"] == "current"
    _fake_velopack(monkeypatch, fail="network down")
    update._fetch("feed")
    assert update.status() == {"state": "failed", "error": "network down"}
    assert update._pending is None
