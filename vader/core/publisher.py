"""목록 배포의 **서명 PC 쪽** — 키 만들기·위임·발행 문서·목록 정보 발행 (신뢰 계약 v2, codex1 2026-10-29 두 번 검토).

마스터 PC 에서만 쓴다. 서버·로그인 없이 동작하고, 서명한 문서는 **먼저 원장에 남긴 뒤** 돌려준다 —
업로드가 실패하면 같은 문서를 다시 보낸다 (같은 순번에 다른 내용을 서명하지 않는다).

- 키는 역할(root·catalog)마다 따로, 암호로 잠근다. 연 키는 `Key`(역할·공개키)로 묶여 다니고, 발행 함수가
  역할과 **지금 위임 상태**(기간·유출·은퇴)를 확인한 뒤에만 서명한다. 거절되면 원장도 바뀌지 않는다.
- 원장은 프로세스 간 잠금(`ledger.lock`) 안에서만 바꾼다. 키는 있는데 원장이 없으면 새로 시작하지 않는다.
- 은퇴 키는 원장에 누적되고, 다음 위임에서 `not_after` 가 은퇴 시각으로 잘린 채 계속 은퇴로 남는다.
- 미승인 룬을 **import 하지 않는다**. 발행 전 검사는 **고정 사본** 하나에만 한다.
- 회수는 누적만 한다. operational → malicious 로 강해질 수는 있어도 사라지거나 약해지지 않는다.
"""
from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import os
import re
import shutil
import tempfile
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from vader.core import catalog

VERSION = re.compile(r"^(0|[1-9]\d{0,3})\.(0|[1-9]\d{0,3})\.(0|[1-9]\d{0,3})$")
MIN_PASSPHRASE = 12
LOCK_WAIT_SECONDS = 10


class PublishError(ValueError):
    """사람에게 그대로 보여 줄 수 있는 거절."""


@dataclass(frozen=True)
class Key:
    """역할이 확인된 열린 키. 발행 함수는 이것만 받는다."""
    role: str
    public_key: str
    private: object


def home() -> Path:
    return Path.home() / ".vaderai-catalog"


# ── 키 ───────────────────────────────────────────────────

def create_key(role: str, path: Path, passphrase: str) -> str:
    """`role`(root|catalog|update) 키를 만들어 `path` 에 잠가 둔다. 공개키(base64)를 돌려준다. 있으면 거절.
    update — 앱 업데이트 피드(releases.win.json) 서명 키 (2026-11-23, SignPath 배포·codex1 업데이트 독립 검증)."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    if role not in ("root", "catalog", "update"):
        raise PublishError("키 역할은 root·catalog·update 입니다")
    if len(passphrase or "") < MIN_PASSPHRASE:
        raise PublishError(f"키 암호는 {MIN_PASSPHRASE}자 이상이어야 합니다")
    private = Ed25519PrivateKey.generate()
    public = _public(private)
    pem = private.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.BestAvailableEncryption(passphrase.encode("utf-8")))
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8") as out:   # 'x' — 두 요청이 겹쳐도 덮어쓰지 않는다
            out.write(json.dumps({"role": role, "public_key": public, "key": pem.decode("ascii")}))
    except FileExistsError:
        raise PublishError(f"이미 키가 있습니다 — 덮어쓰지 않습니다: {path}") from None
    home().mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(FileExistsError):              # 첫 키와 함께 빈 원장 — 그 뒤 원장이 없으면 유실이다
        with (home() / "ledger.json").open("x", encoding="utf-8") as out:
            out.write(json.dumps(_empty()))
    return public


def load_key(role: str, path: Path, passphrase: str) -> Key:
    """역할이 맞고 적힌 공개키와 실제 키가 같은 Ed25519 키만 연다."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise PublishError("키 파일을 읽지 못했습니다") from None
    if data.get("role") != role:
        raise PublishError(f"{role} 키가 아닙니다 (이 파일은 {data.get('role')} 키)")
    try:
        private = serialization.load_pem_private_key(data["key"].encode("ascii"), password=passphrase.encode("utf-8"))
    except (ValueError, KeyError, TypeError):
        raise PublishError("키를 열지 못했습니다 (파일이나 암호를 확인하세요)") from None
    if not isinstance(private, Ed25519PrivateKey) or _public(private) != data.get("public_key"):
        raise PublishError("키 파일의 공개키가 실제 키와 다릅니다")
    return Key(role, data["public_key"], private)


def _public(private) -> str:
    from vader.core.signing import _raw

    return base64.b64encode(_raw(private.public_key())).decode()


def _need(key: Key, role: str) -> None:
    if not isinstance(key, Key) or key.role != role:
        raise PublishError(f"{role} 키로만 발행합니다")


# ── 원장 (프로세스 간 잠금) ──────────────────────────────

@contextlib.contextmanager
def _locked():
    """원장 읽기-고치기-쓰기 한 번을 다른 프로세스와 겹치지 않게 (codex1 P1-4)."""
    home().mkdir(parents=True, exist_ok=True)
    lock = home() / "ledger.lock"
    deadline = time.monotonic() + LOCK_WAIT_SECONDS
    while True:
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            if time.monotonic() > deadline:
                raise PublishError(f"다른 발행이 진행 중입니다. 끝났는데도 남아 있으면 {lock} 를 지우세요.") from None
            time.sleep(0.05)
    try:
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        yield
    finally:
        lock.unlink(missing_ok=True)


def _empty() -> dict:
    return {"delegation": None, "delegation_payload": None, "snapshot": None, "releases": {}, "revocations": {},
            "compromised": [], "retired": {}, "docs": []}


def ledger() -> dict:
    path = home() / "ledger.json"
    if not path.exists():
        # 키는 있는데 원장이 없다 — 처음이 아니라 **유실**이다. 새로 시작하면 같은 순번을 다시 서명한다.
        if any(home().glob("*.key")):
            raise PublishError("키는 있는데 발행 원장이 없습니다. 원장을 백업에서 되살린 뒤 마지막 공개 목록과 대조하세요.")
        return _empty()
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise PublishError("발행 원장이 손상됐습니다. 백업에서 되살린 뒤 다시 하세요.") from None


def _save(data: dict) -> None:
    tmp = home() / f"ledger.{uuid.uuid4().hex}.tmp"
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(home() / "ledger.json")


def mark_published(document: bytes, now: datetime | None = None) -> None:
    """서버가 받은 snapshot 을 적어 둔다 — 만료 안내는 '서명한 것' 이 아니라 '공개된 것' 기준이다 (codex1 7단계 P2-3).
    늦게 온 예전 성공이 새 기록을 덮지 않게, 원장과 같은 잠금 안에서 순번이 오를 때만 쓴다 (7b P2-1)."""
    p = catalog.parse(document)["payload"]
    digest = hashlib.sha256(document).hexdigest()
    with _locked():
        path = home() / "published.json"
        old = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
        if old and (old["epoch"], old["sequence"]) > (p["epoch"], p["sequence"]):
            return
        if old and (old["epoch"], old["sequence"]) == (p["epoch"], p["sequence"]) and old.get("sha256") not in (None, digest):
            raise PublishError("같은 순번에 다른 목록 정보가 공개 기록에 있습니다. 원장과 서버 문서를 대조하세요.")
        state = {f: p[f] for f in ("epoch", "sequence", "issued_at", "expires_at")} | {
            "sha256": digest, "confirmed_at": _iso(now or datetime.now(timezone.utc))}
        tmp = home() / f"published.{uuid.uuid4().hex}.tmp"
        tmp.write_text(json.dumps(state), encoding="utf-8")
        tmp.replace(path)


def renewal_notice(now: datetime | None = None) -> dict | None:
    """서명 PC 에서만 — 받는 PC 의 '갱신 지연' 안내(catalog.notices)는 받은 룬이 있어야 뜨므로 따로 본다.
    위임이 바뀌어 새 목록이 필요함 · 서명만 하고 못 올림(유효하면 push, 만료면 다시 서명) ·
    공개본이 서명 3일 넘음(계약 3절) 이나 곧 만료 — 를 구분한다 (codex1 7b P2-2)."""
    if not (home() / "ledger.json").exists():
        return None
    now = now or datetime.now(timezone.utc)
    resign = "[다시 서명] 을 눌러 목록 키 파일로 새로 서명하세요."   # 화면이 버튼을 붙인다 (action)
    try:
        led = json.loads((home() / "ledger.json").read_text(encoding="utf-8"))
        path = home() / "published.json"
        public = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
        if not isinstance(led, dict) or not isinstance(public, (dict, type(None))) or not led.get("delegation"):
            raise ValueError
        signed = led.get("snapshot")
        if signed is None:
            if led.get("releases") or public:
                return {"rune_id": "", "kind": "renew", "action": "resign", "message": f"위임이 바뀌어 이번 세대의 목록 정보가 없어요. {resign}"}
            return None
        last = [d["payload"] for d in led["docs"] if d["payload"]["purpose"] == "snapshot"][-1]
        if not public or [public["epoch"], public["sequence"]] != list(signed):
            if datetime.fromisoformat(last["expires_at"]) <= now:
                return {"rune_id": "", "kind": "renew", "action": "resign", "message": f"서명만 하고 올리지 못한 목록 정보가 이미 만료됐어요. {resign}"}
            return {"rune_id": "", "kind": "renew", "action": "push",
                    "message": "최신 목록 정보가 공개됐는지 확인되지 않았어요. [다시 올리기] 로 같은 문서를 다시 올리세요."}
        left = datetime.fromisoformat(public["expires_at"]) - now
        if left.total_seconds() <= 0:
            when = "이미 만료됐어요"
        elif now - datetime.fromisoformat(public["issued_at"]) > timedelta(days=3) or left < timedelta(days=2):
            when = f"{max(1, round(left.total_seconds() / 86400))}일 뒤 만료돼요"
        else:
            return None
    except (OSError, ValueError, KeyError, TypeError, IndexError, AttributeError):
        return {"rune_id": "", "kind": "renew", "message": "발행 원장이나 공개 기록을 읽지 못했어요. 서명 PC 의 ~/.vaderai-catalog 를 확인하세요."}
    return {"rune_id": "", "kind": "renew", "action": "resign", "message": f"광장 목록 정보가 {when}. {resign}"}


# ── 목록 정보 다시 서명·올리기 — 앱의 원버튼과 tools/catalog_publish.py 가 같이 쓴다 ──

def publisher_account() -> str:
    """로그인한 계정이 앱의 신뢰 설정에 있는 발행자인지 — 올리거나 키를 열기 전에 (codex1 P1-4).
    발행 키·원장은 운영 하나뿐 — 미리보기 서버에 서명하면 같은 순번에 다른 목록이 생긴다 (codex1 7단계 P1)."""
    from vader.core import account
    if account.site() != account.DEFAULT_SITE:
        raise PublishError(f"광장 발행은 운영 서버({account.DEFAULT_SITE})에서만 합니다. 지금 앱은 {account.site()} 에 연결돼 있습니다.")
    me = account.account()
    if not me:
        raise PublishError("먼저 앱에서 로그인해 주세요.")
    if me["id"] not in catalog.PUBLISHERS:
        who = me.get("display_name") or "이름 없음"
        raise PublishError(f"지금 로그인한 계정({who})은 발행자 계정이 아닙니다. 발행자 계정으로 다시 로그인해 주세요.")
    return me["id"]


def summaries() -> dict:
    """광장에 보일 룬 소개 — `~/.vaderai-catalog/summaries.json` ({"룬 id": "한두 문장"}). 없으면 빈 소개.
    공개되는 문장이다 — 업무 자료·개인정보를 넣지 않는다. 키를 열기 **전에** 검사한다."""
    path = home() / "summaries.json"
    if not path.exists():
        return {}
    try:
        texts = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise PublishError(f"{path} 를 읽지 못했습니다. UTF-8 JSON 인지 확인하세요.") from None
    if not isinstance(texts, dict) or any(not isinstance(k, str) or not isinstance(v, str) or len(v) > 200
                                          for k, v in texts.items()):
        raise PublishError(f'{path} 는 {{"룬 id": "200자 이하 소개"}} 모양이어야 합니다.')
    return texts


def push_latest() -> list[str]:
    """원장에 남은 최신 위임·snapshot 을 다시 올린다 (새로 서명하지 않는다). 줄마다 결과."""
    from vader.core import account
    publisher_account()
    out = []
    for kind in ("delegation", "snapshot"):
        docs = [d for d in ledger()["docs"] if d["payload"]["purpose"] == kind]
        if not docs:
            out.append(f"{kind}: 원장에 아직 없습니다 — 건너뜁니다.")
            continue
        document = catalog.canonical(docs[-1])
        result = account.call("POST", "/api/catalog/documents/",
                              json.dumps({"kind": kind, "document": document.decode("utf-8")}).encode())
        out.append(f"{kind}: " + ("이미 같은 문서가 있습니다." if result.get("already") else "올렸습니다."))
        if kind == "snapshot":
            mark_published(document)         # 서버가 받았다 — 만료 안내의 기준
    return out


def resign(key_file: Path, passphrase: str) -> list[str]:
    """오늘의 목록 정보(snapshot)를 서명하고(원장 먼저) 최신 위임과 함께 올린다 (계약 3절).
    올리다 실패하면 원장에 남은 같은 문서를 push_latest 로 다시 보낸다."""
    publisher_account()
    texts = summaries()
    key = load_key("catalog", key_file, passphrase)
    issue_snapshot(key, summaries=texts)
    return push_latest()


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat()


def _keep(data: dict, doc: dict) -> bytes:
    """서명한 문서를 원장에 먼저 남긴다. 업로드할 바이트를 돌려준다."""
    data["docs"].append(doc)
    _save(data)
    return catalog.canonical(doc)


def _catalog_key_usable(data: dict, key: Key, now: datetime) -> None:
    """지금 위임에서 이 catalog 키로 서명해도 되는가 — 아니면 원장을 건드리기 전에 거절."""
    _need(key, "catalog")
    delegation = data.get("delegation_payload")
    if not delegation:
        raise PublishError("위임 문서를 먼저 발행하세요")
    if catalog._time(delegation["expires_at"]) < now:
        raise PublishError("위임 문서가 만료됐습니다. 새 위임을 먼저 발행하세요")
    if key.public_key in delegation["compromised_keys"] or key.public_key in delegation["retired_keys"]:
        raise PublishError("유출됐거나 은퇴한 키입니다")
    window = [k for k in delegation["catalog_keys"] if k["public_key"] == key.public_key]
    if not window or not catalog._time(window[0]["not_before"]) <= now <= catalog._time(window[0]["not_after"]):
        raise PublishError("지금 위임에 들어 있지 않거나 기간 밖인 키입니다")


# ── 위임 (root) ─────────────────────────────────────────

def issue_delegation(root: Key, catalog_keys: list[dict], *, new_epoch: bool = False,
                     compromised: list[str] = (), retired: list[str] = (), now: datetime | None = None,
                     days: int = 365) -> bytes:
    """root 로 위임 문서를 발행한다. `new_epoch` 는 **키 복구** 때만 — 세대를 올리고 순번을 1 부터.

    `catalog_keys` = `[{"public_key", "not_before", "not_after"}]`. 유출·은퇴는 원장에 누적된다 —
    인자를 빠뜨려도 풀리지 않는다. 은퇴 키는 `not_after` 가 은퇴 시각으로 잘린다 (그 뒤 발행분은 거절).
    """
    _need(root, "root")
    if root.public_key not in catalog.ROOT_KEYS:
        raise PublishError("설치본에 실린 root 키가 아닙니다")
    now = now or datetime.now(timezone.utc)
    if not 1 <= days <= 365:
        raise PublishError("위임 기간은 1~365일입니다")
    with _locked():
        data = ledger()
        last = data["delegation"]
        epoch = (last[0] + 1 if new_epoch else last[0]) if last else catalog.MIN_EPOCH
        sequence = 1 if (not last or new_epoch) else last[1] + 1
        leaked = sorted(set(data["compromised"]) | set(compromised))
        cutoffs = dict(data.get("retired") or {})
        for key in retired:
            cutoffs.setdefault(key, _iso(now))
        entries = []
        for k in catalog_keys:
            if k["public_key"] in leaked:
                continue
            if k["public_key"] in cutoffs and catalog._time(k["not_after"]) > catalog._time(cutoffs[k["public_key"]]):
                k = {**k, "not_after": cutoffs[k["public_key"]]}
            entries.append(k)
        payload = {"schema": 1, "purpose": "delegation", "epoch": epoch, "sequence": sequence,
                   "issued_at": _iso(now), "expires_at": _iso(now + timedelta(days=days)),
                   "catalog_keys": entries, "compromised_keys": leaked, "retired_keys": sorted(cutoffs)}
        doc = catalog.sign("delegation", payload, root.private)
        data.update(delegation=[epoch, sequence], delegation_payload=payload, compromised=leaked, retired=cutoffs)
        if new_epoch:
            data["snapshot"] = None
        return _keep(data, doc)


# ── 발행 문서 (catalog) ─────────────────────────────────

def issue_release(key: Key, archive: Path, *, reviewed_sha256: str, publisher_account_id: str,
                  submission_id: str, revision: int, now: datetime | None = None) -> bytes:
    """심사한 zip 하나의 발행 문서.

    `reviewed_sha256` 은 심사자가 격리 시험 환경에서 확인한 zip digest — **이것과 같은 바이트만** 서명한다.
    모든 검사는 고정 사본 하나에 한다 (원본 경로가 중간에 바뀌어도 상관없다).
    같은 (id, 버전)에 다른 바이트는 서명하지 않는다. 같은 제출의 재전송이면 같은 문서를 돌려준다.
    키 복구로 세대가 올랐으면 같은 바이트를 이번 세대로 다시 승인한다 (악성 회수된 것은 안 된다).
    """
    import zipfile

    from vader.core import installer, profile, release

    now = now or datetime.now(timezone.utc)
    with tempfile.TemporaryDirectory(prefix="vader-publish-") as tmp:
        fixed = Path(tmp) / "package.zip"
        shutil.copyfile(archive, fixed)                         # 고정 사본 — 이후 원본은 다시 열지 않는다
        installer.check_archive(fixed)
        data_bytes = fixed.read_bytes()
        digest = hashlib.sha256(data_bytes).hexdigest()
        if digest != reviewed_sha256:
            raise PublishError("심사한 파일과 다릅니다 (zip digest 불일치)")
        with zipfile.ZipFile(fixed) as book:
            manifest_bytes = book.read("manifest.json")
        manifest = catalog.parse(manifest_bytes)
        if not isinstance(manifest, dict) or not manifest.get("fingerprint") or not manifest.get("tested"):
            raise PublishError("시험 기록이나 지문이 없는 패키지입니다. 제작소에서 시험·포장한 zip 만 받습니다")
        source = installer._unpack(fixed, Path(tmp) / "unpacked")   # 목차·경로·해시·지문 — import 하지 않는다
        leaked = release.check(source)
        if leaked:
            raise PublishError("배포 사본에 넣으면 안 되는 것이 있습니다: " + "; ".join(leaked[:5]))
        rune_id, version = installer._manifest_string(source, "id"), installer._manifest_string(source, "version")
        permissions = sorted(installer._declared(source))
        kind = profile.declared(source) or "legacy"
    if (manifest.get("id"), manifest.get("version")) != (rune_id, version) \
            or sorted(manifest.get("permissions") or []) != permissions or manifest.get("profile") != kind:
        raise PublishError("manifest 와 소스의 선언이 다릅니다")
    if rune_id not in catalog.ALLOWED_IDS or publisher_account_id not in catalog.PUBLISHERS:
        raise PublishError("1차 목록에 올릴 수 있는 룬·발행자가 아닙니다")
    if not VERSION.match(version or ""):
        raise PublishError("버전은 1.2.3 처럼 숫자 세 개만 씁니다")
    with _locked():
        data = ledger()
        _catalog_key_usable(data, key, now)
        epoch = data["delegation"][0]
        name = f"{rune_id}@{version}"
        known = data["releases"].get(name)
        if known and known["archive_sha256"] != digest:
            raise PublishError(f"{name} 는 이미 다른 내용으로 서명했습니다 — 새 버전으로 내세요")
        if (data["revocations"].get(name) or {}).get("level") == "malicious":
            raise PublishError(f"{name} 는 악성으로 회수했습니다 — 다시 승인하지 않습니다")
        current = (known or {}).get("docs", {}).get(str(epoch))
        if current:
            p = current["payload"]
            if (p["submission_id"], p["revision"]) != (submission_id, revision):
                raise PublishError(f"{name} 는 이번 세대에 다른 제출로 이미 승인했습니다")
            return catalog.canonical(current)                   # 같은 제출의 재전송
        payload = {"schema": 1, "purpose": "release", "rune_id": rune_id, "version": version,
                   "submission_id": submission_id, "revision": revision, "archive_sha256": digest,
                   "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(), "size": len(data_bytes),
                   "profile": kind, "permissions": permissions, "publisher_account_id": publisher_account_id,
                   "approved_at": _iso(now), "epoch": epoch}
        doc = catalog.sign("release", payload, key.private)
        entry = known or {"archive_sha256": digest, "name": manifest.get("name", ""), "docs": {}}
        entry["docs"][str(epoch)] = doc
        data["releases"][name] = entry
        return _keep(data, doc)


def revoke(rune_id: str, version: str, level: str, reason: str, now: datetime | None = None) -> None:
    """회수를 원장에 더한다 (다음 snapshot 에 실린다). 약하게 바꾸거나 지우지 않는다."""
    if level not in ("operational", "malicious"):
        raise PublishError("회수 등급은 operational 이나 malicious 입니다")
    with _locked():
        data = ledger()
        name = f"{rune_id}@{version}"
        if name not in data["releases"]:
            raise PublishError(f"발행한 적 없는 버전입니다: {name}")
        old = data["revocations"].get(name)
        if old and old["level"] == "malicious" and level == "operational":
            raise PublishError("악성으로 회수한 버전을 업무 오류로 낮출 수 없습니다")
        data["revocations"][name] = {"rune_id": rune_id, "version": version,
                                     "archive_sha256": data["releases"][name]["archive_sha256"], "level": level,
                                     "reason": reason, "revoked_at": _iso(now or datetime.now(timezone.utc))}
        _save(data)


# ── 목록 정보 (catalog) ─────────────────────────────────

def issue_snapshot(key: Key, *, now: datetime | None = None, days: int = 7, summaries: dict | None = None) -> bytes:
    """이번 세대에 승인한 모든 버전과 누적 회수를 담은 목록 정보. 하루 한 번, 회수가 생기면 바로 (계약 3절)."""
    now = now or datetime.now(timezone.utc)
    if not 1 <= days <= 7:
        raise PublishError("목록 정보 유효기간은 1~7일입니다")
    with _locked():
        data = ledger()
        _catalog_key_usable(data, key, now)
        epoch = data["delegation"][0]
        last = data["snapshot"]
        sequence = last[1] + 1 if last and last[0] == epoch else 1
        releases = []
        for name, item in sorted(data["releases"].items()):
            doc = item.get("docs", {}).get(str(epoch))
            if not doc or doc["payload"]["publisher_account_id"] not in catalog.PUBLISHERS:
                continue
            p = doc["payload"]
            releases.append({f: p[f] for f in ("rune_id", "version", "archive_sha256", "manifest_sha256", "size",
                                               "profile", "permissions")}
                            | {"name": item.get("name", ""), "summary": (summaries or {}).get(p["rune_id"], "")})
        payload = {"schema": 1, "purpose": "snapshot", "epoch": epoch, "sequence": sequence,
                   "issued_at": _iso(now), "expires_at": _iso(now + timedelta(days=days)),
                   "releases": releases, "revocations": [v for _, v in sorted(data["revocations"].items())]}
        doc = catalog.sign("snapshot", payload, key.private)
        data["snapshot"] = [epoch, sequence]
        return _keep(data, doc)
