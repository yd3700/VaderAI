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
    monkeypatch.setattr(update, "UPDATE_KEYS", ())     # 상태 흐름만 본다 — 서명 검사는 test_update_signature
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


def test_verified_sha_only_for_signed_pending(monkeypatch):
    """껍데기는 이 값과 같은 캐시만 적용한다 — 키가 없거나 받아 둔 게 없으면 None (codex1 업데이트 키 리뷰)."""
    info = types.SimpleNamespace(TargetFullRelease=types.SimpleNamespace(SHA256="ABC"))
    monkeypatch.setattr(update, "_pending", None)
    assert update.verified_sha() is None
    monkeypatch.setattr(update, "_pending", (object(), info))
    monkeypatch.setattr(update, "UPDATE_KEYS", ())
    assert update.verified_sha() is None
    monkeypatch.setattr(update, "UPDATE_KEYS", ("k",))
    assert update.verified_sha() == "abc"


def test_apply_on_exit_only_matching_cache(monkeypatch):
    """받아 둔 뒤 캐시 파일이 바뀌었으면 걸지 않는다 (codex1 업데이트 키 리뷰 2차)."""
    applied = []

    class Manager:
        cached = "abc"

        def get_update_pending_restart(self):
            return types.SimpleNamespace(SHA256=self.cached)

        def wait_exit_then_apply_updates(self, asset, **_):
            applied.append(asset.SHA256)

    manager = Manager()
    info = types.SimpleNamespace(TargetFullRelease=types.SimpleNamespace(SHA256="ABC"))
    monkeypatch.setattr(update, "_pending", (manager, info))
    monkeypatch.setattr(update, "UPDATE_KEYS", ("k",))
    manager.cached = "bad"
    update.apply_on_exit()
    assert applied == []
    manager.cached = "ABC"
    update.apply_on_exit()
    assert applied == ["ABC"]


def test_auto_online_off_stops_update_check_and_catalog_refresh(monkeypatch, tmp_path):
    """자동 온라인 확인을 끄면 업데이트 확인·광장 목록 자동 받기를 하지 않는다 (SignPath 조건, 2026-11-24)."""
    from vader.core import catalog_sync

    monkeypatch.setattr(config, "HOME", tmp_path)
    assert config.auto_online() is False                      # 첫 실행 안내 전엔 끔
    config.device_set("network.notice", config.NOTICE_VERSION)
    assert config.auto_online() is True                       # 본 뒤 기본 켬
    started = []
    monkeypatch.setattr(update, "Thread", lambda **kw: types.SimpleNamespace(start=lambda: started.append(kw["name"])))
    monkeypatch.setattr(catalog_sync.threading, "Thread", lambda **kw: types.SimpleNamespace(start=lambda: started.append("catalog")))
    monkeypatch.setattr(config, "FROZEN", True)
    monkeypatch.setattr(update, "UPDATE_KEYS", ("key",))
    monkeypatch.setattr(update, "feed", lambda: "https://example.test/feed")
    monkeypatch.setattr(catalog_sync, "enabled", lambda: True)
    monkeypatch.setitem(catalog_sync._last, "next", 0)

    config.device_set("network.auto_online", False)
    update.start()
    catalog_sync.refresh_if_due()
    assert config.auto_online() is False and started == []

    config.device_set("network.auto_online", None)            # 다시 켜면 지운다 — 기본값으로
    update.start()
    catalog_sync.refresh_if_due()
    assert started == ["vader-update", "catalog"]
    catalog_sync._running.release()                           # refresh_if_due 가 잡은 잠금 — 가짜 스레드는 풀지 않는다
