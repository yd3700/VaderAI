"""업데이트 피드 독립 서명 (2026-11-23) — 고정 키로 서명된 목록의 sha256 과 같은 패키지만 받는다."""
import base64
import json
from types import SimpleNamespace as NS

import pytest

pytest.importorskip("cryptography")
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat  # noqa: E402

from vader.core import update  # noqa: E402

KEY = Ed25519PrivateKey.generate()
PUB = base64.b64encode(KEY.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode()
MANIFEST = json.dumps({"Assets": [{"FileName": "VaderAI-0.2.0-full.nupkg", "SHA256": "AB" * 32},
                                  {"FileName": "VaderAI-0.2.0-delta.nupkg", "SHA256": "cd" * 32}]}).encode()
SIG = base64.b64encode(KEY.sign(MANIFEST))
asset = lambda name, sha: NS(FileName=name, SHA256=sha)   # noqa: E731
INFO = NS(TargetFullRelease=asset("VaderAI-0.2.0-full.nupkg", "ab" * 32),
          DeltasToTarget=[asset("VaderAI-0.2.0-delta.nupkg", "CD" * 32)])


def test_a_signed_manifest_that_lists_the_packages_passes():
    update.check_signed(INFO, MANIFEST, SIG, keys=(PUB,))


@pytest.mark.parametrize("manifest, sig, info, why", [
    (MANIFEST + b" ", SIG, INFO, "서명이 맞지 않"),                                  # 목록이 바뀌었다
    (MANIFEST, base64.b64encode(Ed25519PrivateKey.generate().sign(MANIFEST)), INFO, "서명이 맞지 않"),  # 다른 키
    (MANIFEST, b"not base64!", INFO, "읽지 못했"),
    (MANIFEST, SIG, NS(TargetFullRelease=asset("VaderAI-0.2.0-full.nupkg", "ee" * 32), DeltasToTarget=[]), "목록에 없는"),
    (MANIFEST, SIG, NS(TargetFullRelease=INFO.TargetFullRelease, DeltasToTarget=[asset("evil-delta.nupkg", "cd" * 32)]), "목록에 없는"),
])
def test_anything_else_is_refused(manifest, sig, info, why):
    with pytest.raises(update.UpdateRefused, match=why):
        update.check_signed(info, manifest, sig, keys=(PUB,))


def test_updates_stay_off_until_an_update_key_ships(monkeypatch):
    """서명 키가 없는 판은 어떤 피드든 켜지 않는다 — 기기 설정의 다른 주소로도 (codex1 구현 2차 P1)."""
    started = []
    monkeypatch.setattr(update.config, "FROZEN", True)
    monkeypatch.setattr(update, "feed", lambda: "https://example.invalid/other-feed")
    monkeypatch.setattr(update, "Thread", lambda **k: started.append(k) or NS(start=lambda: None))
    monkeypatch.setattr(update, "UPDATE_KEYS", ())
    update.start()
    assert not started
    monkeypatch.setattr(update, "UPDATE_KEYS", (PUB,))
    update.start()
    assert len(started) == 1
