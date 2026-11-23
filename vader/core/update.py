"""자동 업데이트 — 설치본(Velopack)에서만 돈다 (2026-10-21 런칭 0단계, codex1 합의).

켤 때 뒤에서 확인·다운로드만 해 두고, **창을 닫을 때** 적용한다. 작업 중에는 창이 닫히지 않으므로
(`DesktopController.begin_close`) 적용 시점이 곧 "실행 중인 Job 이 없는 때" 다. 실행 중에 바꾸지 않는다.

피드 주소는 `update.feed` (폴더 경로 또는 https 주소). 없으면 설치본에 실린 `update_feed.txt` 를 쓴다
(packaging/build.ps1 이 비공개 저장소 주소를 넣는다 — 저장소에 올리지 않는 파일. 2026-10-26 마스터 결정).
둘 다 없으면 아무것도 하지 않는다. 업데이트가 실패해도 앱은 그대로 켜진다 — 업데이트 때문에 업무를 못 하면 안 된다.
"""
from __future__ import annotations

from threading import Thread

from vader.core import config

#: 업데이트 피드 서명 공개키(Ed25519 base64, `tools/update_sign.py`). 하나라도 있으면 피드의 releases.win.json 이
#: 이 키로 서명돼 있고, 받을 패키지가 그 목록의 sha256 과 같을 때만 받는다 — 피드·저장소가 함께 털려도 막는다 (codex1 2026-11-20).
UPDATE_KEYS: tuple[str, ...] = ("e3+sWaPhJPraSN5F/U5iVWPnySs+BJEOUAj+NUkV4xw=",)   # 마스터 2026-11-23
MANIFEST = "releases.win.json"

_pending: tuple | None = None   # (UpdateManager, UpdateInfo) — 받아 두고 적용을 기다리는 것
#: 화면이 읽는 상태. off · checking · downloading · ready · current · failed
_status: dict = {"state": "off"}


def feed() -> str | None:
    """설정이 우선, 없으면 설치본에 실린 주소."""
    configured = config.device_get("update.feed")
    if configured:
        return configured
    bundled = config.TEMPLATE_PATH.parent / "update_feed.txt"
    try:
        return bundled.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def status() -> dict:
    return dict(_status)


def start() -> None:
    """뒤에서 새 버전을 확인하고 받아 둔다."""
    source = feed() if config.FROZEN else None
    if not UPDATE_KEYS:
        source = None   # 서명 키가 실린 판만 업데이트한다 — 피드 주소와 상관없이, 키 준비 전에는 끈다 (codex1 구현 2차 P1)
    if source:
        _status.update(state="checking")
        Thread(target=_fetch, args=(source,), name="vader-update", daemon=True).start()


def _fetch(source: str) -> None:
    global _pending
    try:
        import velopack
        manager = velopack.UpdateManager(source)
        info = manager.check_for_updates()
        if info is None:
            _status.update(state="current")
            return
        if UPDATE_KEYS:
            check_signed(info, *_read_manifest(source))
        _status.update(state="downloading", version=str(info.TargetFullRelease.Version))
        manager.download_updates(info, lambda percent, *_: _status.update(progress=int(percent)))
        _pending = (manager, info)
        _status.update(state="ready")
    except Exception as exc:
        # 실패는 화면 머리띠에 조용히 알리고, 다음 실행 때 처음부터 다시 시도한다
        _status.update(state="failed", error=str(exc)[:200])


class UpdateRefused(RuntimeError):
    """서명이 없거나 맞지 않는 업데이트 — 받지 않고 지금 버전을 그대로 쓴다."""


def _read_manifest(source: str) -> tuple[bytes, bytes]:
    """피드의 목록과 서명. https 주소면 받고, 폴더면 읽는다."""
    if source.startswith("https://"):
        from vader.core.http import HttpGateway   # 새 네트워크 문을 열지 않는다 — 데이터 지도의 그 문으로

        def get(name):
            answer = HttpGateway().get(f"{source.rstrip('/')}/{name}")
            if answer.status != 200:
                raise UpdateRefused(f"업데이트 {name} 을 받지 못했습니다 ({answer.status})")
            return answer.text.encode("utf-8")   # 잘렸거나 바뀌었으면 아래 서명 검사에서 걸린다
        return get(MANIFEST), get(MANIFEST + ".sig")
    from pathlib import Path
    folder = Path(source)
    return (folder / MANIFEST).read_bytes(), (folder / (MANIFEST + ".sig")).read_bytes()


def check_signed(info, manifest: bytes, signature: bytes, keys: tuple[str, ...] | None = None) -> None:
    """서명이 고정 키 중 하나로 맞고, 받을 전체·차분 패키지가 모두 그 목록의 sha256 과 같은지. 아니면 UpdateRefused."""
    import base64
    import json

    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    try:
        sig = base64.b64decode(signature.strip(), validate=True)
    except ValueError:
        raise UpdateRefused("업데이트 서명을 읽지 못했습니다") from None
    for key in (UPDATE_KEYS if keys is None else keys):
        try:
            Ed25519PublicKey.from_public_bytes(base64.b64decode(key)).verify(sig, manifest)
            break
        except (InvalidSignature, ValueError):
            continue
    else:
        raise UpdateRefused("업데이트 서명이 맞지 않습니다 — 받지 않습니다")
    signed = {a.get("FileName"): str(a.get("SHA256") or "").lower()
              for a in json.loads(manifest.decode("utf-8")).get("Assets", [])}
    for asset in [info.TargetFullRelease, *(info.DeltasToTarget or [])]:
        if not asset.SHA256 or signed.get(asset.FileName) != str(asset.SHA256).lower():
            raise UpdateRefused(f"서명된 목록에 없는 업데이트 파일입니다: {asset.FileName}")


def verified_sha() -> str | None:
    """이번 실행에서 서명 검증을 거쳐 받아 둔 전체 패키지의 sha256 — 껍데기는 캐시 파일이 이 값과 같을 때만 적용한다."""
    if _pending and UPDATE_KEYS:
        return str(_pending[1].TargetFullRelease.SHA256).lower()
    return None


def apply_on_exit() -> None:
    """창이 닫힌 뒤 부른다. 이 프로세스가 끝나면 Velopack 이 교체한다 (다시 켜지 않는다)."""
    verified = verified_sha()
    if verified:   # 껍데기와 같은 규칙 — 캐시 파일의 실제 sha256(Velopack 이 파일에서 계산)이 검증값과 같을 때만 (codex1 업데이트 키 리뷰 2차)
        manager = _pending[0]
        asset = manager.get_update_pending_restart()
        if asset and str(asset.SHA256).lower() == verified:
            manager.wait_exit_then_apply_updates(asset, silent=True, restart=False)
