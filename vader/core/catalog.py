"""목록 배포의 **받는 PC 신뢰 코어** — 서명 문서·위임·snapshot·회수·판정 (신뢰 계약 v2 2~6절, 2026-10-29).

외부 문구에 쓰지 않는 내부 이름이다. 서버·로그인 없이 동작한다: 문서는 바이트로 받아 여기서 검증한다.

- 서명 문서 = `{"payload", "signature", "public_key"}`. 서명 대상 = `b"vaderai-catalog\\n" + purpose + b"\\n" + 정규JSON`.
- root 공개키는 설치본에 싣는다(`ROOT_KEYS`). 비어 있으면 목록 기능이 꺼진 것이다.
- **받을 때와 캐시를 읽을 때 같은 검증 함수를 쓴다** (codex1 P1-1).
- 상태 파일 — 회수·되돌리기 방지는 **기기 층**(`HOME/catalog/`, 계정을 바꿔도 초기화되지 않는다),
  설치 원장(managed·origins)은 **계정 층**(`<data_dir>/catalog/`) (2단계 codex1 분류, 2026-11-05):
    `delegation.json`·`snapshot.json`  받은 문서 캐시 — 지워도 된다
    `state.json`      마지막으로 받아들인 (세대, 순번, digest). **캐시와 따로** — 지워도 되돌리기를 막는다 (P1-2)
    `blocked.json`    악성·유출 대상 (zip digest·설치 지문). 문서를 저장하기 **전에** 쓴다 (P1-3)
    `managed.json`    목록이 관리하는 룬 id. 출처 원장이 사라져도 일반 룬으로 바뀌지 않게 (P1-4)
    `origins.json`    출처 원장
  상태 파일이 깨졌으면 빈 상태가 아니라 **복구 필요**다 — 목록 룬을 막는다.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from vader.core import config

#: 설치본에 싣는 root 공개키(base64). 키를 만들면 여기에 넣고 빌드한다 — 비면 목록 기능이 꺼진다.
ROOT_KEYS: tuple[str, ...] = ("hcCZtwtGJdHF8T3/zh7aU/Ju+BMihr1+ST5kn8Eo4Do=",)  # 2026-10-30, 오프라인 USB
#: 이 설치본이 받아들이는 가장 낮은 위임 세대. 키를 복구하면 올려서 빌드한다.
MIN_EPOCH = 1
#: 1차 발행자 계정과 목록 id (계약 10절). 서명이 맞아도 이 밖이면 설치하지 않는다.
#  운영 DB 의 마스터 계정만. 계정 id 는 DB 마다 따로 생기고, snapshot 은 이 계정이 승인한 것만 싣는다 —
#  미리보기 계정을 같이 두면 리허설 승인이 운영 목록에 섞인다 (2026-10-30). 미리보기 시험 때만 잠깐 바꿔 쓴다.
PUBLISHERS: tuple[str, ...] = ("c26b2048-d311-4fcb-af71-b713433204ff",)
ALLOWED_IDS: tuple[str, ...] = ("eonm_ipgo", "ins_singo", "supply_weekly")
SNAPSHOT_MAX = timedelta(days=7)
DELEGATION_MAX = timedelta(days=366)
CLOCK_SKEW = timedelta(minutes=5)
MAX_INT = 2**53 - 1
MAX_DOC_BYTES = 1024 * 1024
PURPOSES = {"release", "snapshot", "delegation"}
_LOCK = threading.Lock()
#: 목록 정보가 바뀌면 부를 것 — runner 가 실행 중 작업을 다시 판정한다 (계약 8절). 약한 참조로 든다.
_LISTENERS: list = []
#: 만료 며칠 전부터 갱신이 늦어진다고 알린다 (계약 3절)
STALE_WARNING = timedelta(days=2)


class CatalogError(ValueError):
    """사람에게 그대로 보여 줄 수 있는 거절."""


class CorruptState(CatalogError):
    """상태 파일이 깨졌다 — 빈 상태로 보지 않는다."""


def on_change(method) -> None:
    """목록 정보가 바뀌면 `method()` 를 부른다. 바운드 메서드만 — 주인이 사라지면 같이 빠진다."""
    import weakref

    _LISTENERS.append(weakref.WeakMethod(method))


def _changed() -> None:
    for ref in list(_LISTENERS):
        method = ref()
        if method is None:
            _LISTENERS.remove(ref)
            continue
        try:
            method()
        except Exception:  # noqa: BLE001 — 알림 하나가 실패해도 목록 수신은 끝난 것이다
            pass


# ── 정규 JSON ─────────────────────────────────────────────

def canonical(value) -> bytes:
    """정규 JSON 바이트. 실수·null·범위 밖 정수는 거절한다 (Python·Node 가 같게 읽는 것만)."""
    _check_types(value)
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    except UnicodeEncodeError:
        raise CatalogError("짝이 없는 서로게이트 문자") from None


def parse(raw: bytes):
    """같은 키가 둘인 JSON 은 거절한다 — 구현마다 다르게 읽는다."""
    if len(raw) > MAX_DOC_BYTES:
        raise CatalogError("문서가 너무 큽니다")

    def no_duplicates(pairs):
        keys = [k for k, _ in pairs]
        if len(keys) != len(set(keys)):
            raise CatalogError("같은 키가 두 번 있는 문서입니다")
        if "__proto__" in keys:    # Node 객체에서 특별하게 읽히는 키 — 양쪽 모두 거절한다 (codex1 P1-1)
            raise CatalogError("쓸 수 없는 키가 있습니다: __proto__")
        return dict(pairs)
    def no_number(text):     # 소수·지수·NaN·-0 — Node 쪽(web/server/catalog-canonical.mjs)과 같이 거절
        raise CatalogError("정수는 0 ~ 2^53-1 만 씁니다")

    def whole(text):
        if text.startswith("-"):
            no_number(text)
        return int(text)
    try:
        return json.loads(raw, object_pairs_hook=no_duplicates, parse_float=no_number, parse_int=whole,
                          parse_constant=no_number)
    except (ValueError, RecursionError) as exc:
        raise CatalogError(f"문서를 읽지 못했습니다: {exc}") from None


def _check_types(value) -> None:
    if isinstance(value, bool) or isinstance(value, str):
        return
    if isinstance(value, int):
        if not 0 <= value <= MAX_INT:
            raise CatalogError("정수는 0 ~ 2^53-1 만 씁니다")
        return
    if isinstance(value, list):
        for item in value:
            _check_types(item)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str) or key == "__proto__":
                raise CatalogError("키는 문자열이어야 하고 __proto__ 는 쓸 수 없습니다")
            _check_types(item)
        return
    raise CatalogError(f"문서에 쓸 수 없는 값입니다: {type(value).__name__}")


def _message(purpose: str, payload: dict) -> bytes:
    return b"vaderai-catalog\n" + purpose.encode("ascii") + b"\n" + canonical(payload)


def _digest(payload: dict) -> str:
    return hashlib.sha256(canonical(payload)).hexdigest()


# ── 스키마 (P2-3) ─────────────────────────────────────────

def _int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _hex64(value) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def _time(text) -> datetime:
    try:
        moment = datetime.fromisoformat(text)
    except (TypeError, ValueError):
        raise CatalogError("시각 형식이 맞지 않습니다") from None
    if moment.tzinfo is None:
        raise CatalogError("시각에 시간대가 없습니다")
    return moment


def _need(payload: dict, fields: dict) -> None:
    for name, check in fields.items():
        if name not in payload or not check(payload[name]):
            raise CatalogError(f"문서의 {name} 값이 없거나 맞지 않습니다")


def _times(*values) -> bool:
    for value in values:
        _time(value)
    return True


_KEY_ENTRY = {"public_key": lambda v: isinstance(v, str), "not_before": _times, "not_after": _times}
_RELEASE_ENTRY = {"rune_id": lambda v: isinstance(v, str), "version": lambda v: isinstance(v, str),
                  "archive_sha256": _hex64, "manifest_sha256": _hex64, "size": _int,
                  "profile": lambda v: isinstance(v, str),
                  "permissions": lambda v: isinstance(v, list) and all(isinstance(p, str) for p in v)}
_REVOCATION = {"archive_sha256": _hex64, "level": lambda v: v in ("operational", "malicious"),
               "reason": lambda v: isinstance(v, str)}


def _list_of(spec):
    def check(value):
        if not isinstance(value, list):
            return False
        for item in value:
            if not isinstance(item, dict):
                return False
            _need(item, spec)
        return True
    return check


_SCHEMAS = {
    "delegation": {"epoch": _int, "sequence": _int, "issued_at": _times, "expires_at": _times,
                   "catalog_keys": _list_of(_KEY_ENTRY),
                   "compromised_keys": lambda v: isinstance(v, list) and all(isinstance(k, str) for k in v),
                   "retired_keys": lambda v: isinstance(v, list) and all(isinstance(k, str) for k in v)},
    "snapshot": {"epoch": _int, "sequence": _int, "issued_at": _times, "expires_at": _times,
                 "releases": _list_of(_RELEASE_ENTRY), "revocations": _list_of(_REVOCATION)},
    "release": {**_RELEASE_ENTRY, "epoch": _int, "approved_at": _times,
                "publisher_account_id": lambda v: isinstance(v, str), "submission_id": lambda v: isinstance(v, str),
                "revision": _int},
}


# ── 서명·검증 ─────────────────────────────────────────────

def sign(purpose: str, payload: dict, private_key) -> dict:
    """서명 PC 가 쓴다. `private_key` 는 Ed25519 개인키 객체 (암호로 연 것)."""
    from vader.core.signing import _raw

    if purpose not in PURPOSES or payload.get("purpose") != purpose or payload.get("schema") != 1:
        raise CatalogError("문서 목적·schema 가 맞지 않습니다")
    _need(payload, _SCHEMAS[purpose])
    return {"payload": payload, "signature": base64.b64encode(private_key.sign(_message(purpose, payload))).decode(),
            "public_key": base64.b64encode(_raw(private_key.public_key())).decode()}


def verify(doc, purpose: str, allowed_keys: set[str]) -> dict:
    """서명이 맞고 그 키가 `allowed_keys`(base64 공개키) 안이면 스키마를 본 payload 를 돌려준다."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    if not isinstance(doc, dict) or set(doc) != {"payload", "signature", "public_key"} \
            or not isinstance(doc["public_key"], str) or not isinstance(doc["signature"], str):
        raise CatalogError("서명 문서 모양이 아닙니다")
    payload, key = doc["payload"], doc["public_key"]
    if not isinstance(payload, dict) or not _int(payload.get("schema")) or payload["schema"] != 1 \
            or payload.get("purpose") != purpose:
        raise CatalogError(f"{purpose} 문서가 아니거나 모르는 schema 입니다")
    if key not in allowed_keys:
        raise CatalogError("믿을 수 있는 키가 서명하지 않았습니다")
    try:
        Ed25519PublicKey.from_public_bytes(base64.b64decode(key)).verify(
            base64.b64decode(doc["signature"]), _message(purpose, payload))
    except (InvalidSignature, ValueError, TypeError):
        raise CatalogError("서명이 맞지 않습니다") from None
    _need(payload, _SCHEMAS[purpose])
    return payload


# ── 상태 파일 ─────────────────────────────────────────────

_ACCOUNT_FILES = ("managed.json", "origins.json")


def _file(name: str) -> Path:
    return (config.data_dir() if name in _ACCOUNT_FILES else config.HOME) / "catalog" / name


def _read(name: str, default):
    """없으면 `default`. **있는데 깨졌으면 CorruptState** — 빈 상태로 보지 않는다."""
    path = _file(name)
    if not path.exists():
        return default
    try:
        data = parse(path.read_bytes())
    except (OSError, CatalogError):
        raise CorruptState(f"목록 상태 파일 {name} 이 손상됐습니다") from None
    if not isinstance(data, type(default)):
        raise CorruptState(f"목록 상태 파일 {name} 이 손상됐습니다")
    return data


def _write(name: str, data) -> None:
    path = _file(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{name}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _entries(origins_: list, reason: str) -> dict:
    """설치본마다 zip digest 와 설치 지문 둘 다 막는다 — 폴더로 다시 넣어도 걸리게 (P1-5)."""
    out = {}
    for o in origins_:
        if o.get("archive_sha256"):
            out[o["archive_sha256"]] = reason
        if o.get("fingerprint"):
            out[f"fp:{o['fingerprint']}"] = reason
    return out


def _block(entries: dict) -> None:
    """차단은 누적만 한다. 문서를 저장하기 **전에** 부른다."""
    if entries:
        blocked = _read("blocked.json", {})
        _write("blocked.json", {**entries, **blocked})


# ── 위임 ─────────────────────────────────────────────────

def _check_delegation(payload: dict, now: datetime) -> None:
    issued, expires = _time(payload["issued_at"]), _time(payload["expires_at"])
    if payload["epoch"] < MIN_EPOCH:
        raise CatalogError("이 설치본이 받지 않는 오래된 세대의 위임입니다")
    if not issued <= expires or expires - issued > DELEGATION_MAX or issued > now + CLOCK_SKEW or expires < now:
        raise CatalogError("위임 문서의 기간이 맞지 않거나 만료됐습니다")


def accept_delegation(raw: bytes, now: datetime | None = None) -> dict:
    """root 가 서명한 위임 문서를 받아들인다. snapshot 과 **독립적으로 먼저** 반영한다 (계약 2.4)."""
    if not ROOT_KEYS:
        raise CatalogError("이 설치본에는 목록 root 키가 없습니다")
    now = now or _now()
    with _LOCK:
        payload = verify(parse(raw), "delegation", set(ROOT_KEYS))
        _check_delegation(payload, now)
        state = _read("state.json", {})
        seen = state.get("delegation")
        here = [payload["epoch"], payload["sequence"]]
        if seen and here < seen[:2]:
            raise CatalogError("이미 받은 것보다 오래된 위임 문서입니다")
        if seen and here == seen[:2] and _digest(payload) != seen[2]:
            raise CatalogError("같은 순번에 내용이 다른 위임 문서입니다")
        # 유출 키가 서명한 설치본은 악성 등급 — 문서 저장 **전에** (같은 문서를 다시 받아도 다시 적용)
        compromised = set(payload["compromised_keys"])
        # 키 자체도 기기 층에 누적한다 — 지금 안 열린 다른 계정의 설치본도, 뒤 위임이 목록에서 빼도 막히게 (codex1 P1-6)
        _write("compromised.json", sorted(compromised | set(_read("compromised.json", []))))
        _block(_entries([o for o in _read("origins.json", {}).values()
                         if isinstance(o, dict) and o.get("public_key") in compromised], "서명 키 유출"))
        _write("delegation.json", parse(raw))
        state["delegation"] = [*here, _digest(payload)]
        if seen and here[0] > seen[0]:
            state.pop("snapshot", None)          # 세대가 오를 때만 snapshot 순번을 다시 센다
        _write("state.json", state)
    _changed()
    return payload


def current_delegation(now: datetime | None = None) -> dict | None:
    """지금 쓸 수 있는 위임 — 서명·세대·기간·마지막 수락 기록까지 다시 본다."""
    if not ROOT_KEYS:
        return None
    now = now or _now()
    try:
        doc = _read("delegation.json", {})
        if not doc:
            return None
        payload = verify(doc, "delegation", set(ROOT_KEYS))
        _check_delegation(payload, now)
        seen = _read("state.json", {}).get("delegation")
    except CorruptState:
        raise
    except CatalogError:
        return None
    if not seen or [payload["epoch"], payload["sequence"], _digest(payload)] != seen:
        return None                               # 캐시가 마지막으로 받은 것이 아니다
    return payload


def _signing_keys(delegation: dict, at: datetime) -> set[str]:
    """그 시각에 서명할 수 있는 목록 키 — 위임 기간 안, 유출·은퇴 아님."""
    bad = set(delegation["compromised_keys"]) | set(delegation["retired_keys"])
    return {k["public_key"] for k in delegation["catalog_keys"]
            if k["public_key"] not in bad and _time(k["not_before"]) <= at <= _time(k["not_after"])}


# ── 목록 정보 ─────────────────────────────────────────────

def _check_snapshot(doc, delegation: dict, now: datetime) -> dict:
    payload = doc["payload"] if isinstance(doc, dict) and isinstance(doc.get("payload"), dict) else {}
    try:
        issued = _time(payload.get("issued_at"))
    except CatalogError:
        raise CatalogError("목록 정보의 시각이 맞지 않습니다") from None
    payload = verify(doc, "snapshot", _signing_keys(delegation, issued))
    expires = _time(payload["expires_at"])
    if not issued <= expires or expires - issued > SNAPSHOT_MAX or issued > now + CLOCK_SKEW or expires < now:
        raise CatalogError("목록 정보의 시각이 맞지 않거나 만료됐습니다")
    if payload["epoch"] != delegation["epoch"]:
        raise CatalogError("목록 정보의 세대가 위임 문서와 다릅니다")
    pairs = [(r["rune_id"], r["version"]) for r in payload["releases"]]
    if len(pairs) != len(set(pairs)):
        raise CatalogError("목록 정보에 같은 버전이 두 번 있습니다")
    return payload


def accept_snapshot(raw: bytes, now: datetime | None = None) -> dict:
    """목록 snapshot 을 받아들인다. 악성 회수는 `blocked.json` 에 먼저 영구히 남긴다."""
    now = now or _now()
    with _LOCK:
        delegation = current_delegation(now)
        if delegation is None:
            raise CatalogError("위임 문서가 없어 목록을 확인할 수 없습니다")
        payload = _check_snapshot(parse(raw), delegation, now)
        state = _read("state.json", {})
        seen = state.get("snapshot")
        here = [payload["epoch"], payload["sequence"]]
        if seen and here < seen[:2]:
            raise CatalogError("이미 받은 것보다 오래된 목록 정보입니다")
        if seen and here == seen[:2] and _digest(payload) != seen[2]:
            raise CatalogError("같은 순번에 내용이 다른 목록 정보입니다")
        bad = {r["archive_sha256"] for r in payload["revocations"] if r["level"] == "malicious"}
        known = [o for o in _read("origins.json", {}).values() if isinstance(o, dict) and o.get("archive_sha256") in bad]
        _block({**{d: "악성 회수" for d in bad}, **_entries(known, "악성 회수")})
        _write("snapshot.json", parse(raw))
        state["snapshot"] = [*here, _digest(payload)]
        _write("state.json", state)
    _changed()
    return payload


def current_snapshot(now: datetime | None = None) -> dict | None:
    """지금 쓸 수 있는 snapshot. 없음·검증 실패·만료·마지막 수락본이 아님이면 None."""
    now = now or _now()
    delegation = current_delegation(now)
    if delegation is None:
        return None
    try:
        doc = _read("snapshot.json", {})
        if not doc:
            return None
        payload = _check_snapshot(doc, delegation, now)
        seen = _read("state.json", {}).get("snapshot")
    except CorruptState:
        raise
    except CatalogError:
        return None
    if not seen or [payload["epoch"], payload["sequence"], _digest(payload)] != seen:
        return None
    return payload


# ── 출처 원장 ─────────────────────────────────────────────

def origins() -> dict:
    """목록에서 설치한 룬의 출처 원장. 깨졌으면 CorruptState."""
    return _read("origins.json", {})


def managed() -> list:
    return _read("managed.json", [])


def record_origin(rune_id: str, entry: dict) -> None:
    with _LOCK:
        ids = managed()
        if rune_id not in ids:
            _write("managed.json", [*ids, rune_id])   # 관리 표시를 먼저 — 원장이 사라져도 목록 룬으로 남는다
        data = origins()
        data[rune_id] = entry
        _write("origins.json", data)


def _restore_origin(rune_id: str, previous: dict | None) -> None:
    with _LOCK:
        data = origins()
        if previous is None:
            data.pop(rune_id, None)
            _write("managed.json", [i for i in managed() if i != rune_id])
        else:
            data[rune_id] = previous
        _write("origins.json", data)


def local_rune(rune_id: str, rune_dir: Path | None = None) -> bool:
    """목록과 무관하게 직접 설치한 룬이 있는가. 목록 기록(관리 표시·출처)이 하나라도 있으면 목록 룬이다 —
    기록이 반쯤 남은 것은 '다시 받기' 로 고친다. 빈 폴더·잔여 폴더는 룬이 아니다 (codex1 6단계 P2-2·P2-4)."""
    return (rune_id not in managed() and rune_id not in origins()
            and ((rune_dir or config.rune_dir()) / rune_id / "__init__.py").is_file())


def blocked_install(archive_sha256: str = "", fingerprint: str = "") -> str:
    """일반 설치도 막아야 하는 대상인가. 이유를 돌려준다 (없으면 빈 문자열)."""
    blocked = _read("blocked.json", {})
    return (archive_sha256 and blocked.get(archive_sha256, "")) or (fingerprint and blocked.get(f"fp:{fingerprint}", "")) or ""


# ── 판정표 (계약 5절) ─────────────────────────────────────

ALLOW_ALL = {"import": True, "practice": True, "live": True, "reason": "", "level": "ok"}


def verdict(rune_id: str, now: datetime | None = None) -> dict:
    """불러오기·연습·실전을 해도 되는지. 화면·설치·discover·연습·실전·재개가 모두 이것을 쓴다.

    목록이 관리하지 않는 룬은 지금처럼 둔다 (봉인은 기존 `seal` 이 본다).
    """
    def deny(reason, practice=False, level="unavailable"):
        """`level`: malicious(악성·키 유출) · recalled(업무 오류 회수) · expired · pending · corrupt · unavailable"""
        return {"import": practice, "practice": practice, "live": False, "reason": reason, "level": level}

    try:
        if rune_id not in managed():
            if rune_id in origins():          # 출처는 남았는데 관리 표시만 없다 — 유실이지 일반 룬이 아니다 (P1-7)
                return deny("목록 관리 기록이 손상됐습니다. 목록에서 다시 받으세요.", level="corrupt")
            return dict(ALLOW_ALL)
        origin = origins().get(rune_id)
        if not isinstance(origin, dict) or origin.get("state") != "installed" or not origin.get("archive_sha256"):
            return deny("목록 설치가 끝나지 않았거나 기록이 없습니다. 다시 받으세요.", level="pending")
        from vader.core import seal

        installed = seal.fingerprint(config.rune_dir() / rune_id)
        if installed != origin.get("fingerprint"):
            return deny("설치된 파일이 목록에서 받은 것과 다릅니다. 다시 받으세요.")
        reason = blocked_install(origin["archive_sha256"], installed)
        if reason:
            return deny(f"사용할 수 없는 버전입니다 ({reason}).", level="malicious")
        delegation = current_delegation(now)
        leaked = set(_read("compromised.json", [])) | set(delegation["compromised_keys"] if delegation else [])
        if origin.get("public_key") in leaked:
            return deny("사용할 수 없는 버전입니다 (서명 키 유출).", level="malicious")
        snapshot = current_snapshot(now)
        if snapshot is None:
            return deny("목록 정보가 없거나 만료됐습니다. 인터넷에 연결해 목록 정보를 받으세요.", level="expired")
        for revoked in snapshot["revocations"]:
            if revoked["archive_sha256"] == origin["archive_sha256"]:
                operational = revoked["level"] == "operational"
                return deny(f"회수된 버전입니다: {revoked['reason']}", practice=operational,
                            level="recalled" if operational else "malicious")
        return dict(ALLOW_ALL)
    except CorruptState as exc:
        return deny(f"{exc}. 목록 정보를 다시 받거나 관리자에게 알리세요.", level="corrupt")
    except (CatalogError, KeyError, TypeError, OSError) as exc:
        return deny(f"목록 정보를 확인하지 못했습니다: {exc}")


def _version(text: str) -> tuple:
    try:
        return tuple(int(p) for p in str(text).split("."))
    except ValueError:
        return ()


def notices(now: datetime | None = None) -> list[dict]:
    """화면에 띄울 목록 안내 — 새 버전·회수(실전만 막힘)·갱신 지연. 불러오기가 막힌 룬은
    이미 '불러오지 못한 룬' 에 사유가 있으므로 여기서 뺀다."""
    now = now or _now()
    out = []
    try:
        ids = managed()
        snapshot = current_snapshot(now) if ids else None
        for rune_id in ids:
            allowed = verdict(rune_id, now)
            if not allowed["import"]:
                continue
            if not allowed["live"]:
                out.append({"rune_id": rune_id, "kind": "recalled", "message": allowed["reason"]})
            origin = origins().get(rune_id) or {}
            revoked = {r["archive_sha256"] for r in snapshot["revocations"]} if snapshot else set()
            newer = sorted((r for r in (snapshot or {}).get("releases", [])
                            if r["rune_id"] == rune_id and r["archive_sha256"] not in revoked
                            and _version(r["version"]) > _version(origin.get("version", ""))),
                           key=lambda r: _version(r["version"]))
            if newer:
                out.append({"rune_id": rune_id, "kind": "update", "version": newer[-1]["version"],
                            "message": f"새 버전 v{newer[-1]['version']} 이 나왔어요."})
        if snapshot and _time(snapshot["expires_at"]) - now < STALE_WARNING:
            days = max(0, (_time(snapshot["expires_at"]) - now).days)
            out.append({"rune_id": "", "kind": "stale",
                        "message": f"목록 정보 갱신이 늦어지고 있어요. {days}일 안에 받지 못하면 목록에서 받은 룬이 멈춥니다."})
    except CatalogError:
        return out
    return out


# ── 받아 설치 (계약 6절) ─────────────────────────────────

def check_release(release_raw: bytes, now: datetime | None = None, expect: tuple[str, str] | None = None):
    """발행 문서를 **받기 전에** 판정한다 — 서명·위임 키 기간·세대·발행자·목록 등재·회수 (codex1 5단계 P1-2·P1-3).

    `(doc, release, listed)` 를 돌려준다. `expect=(rune_id, version)` 이면 사람이 고른 것과 같아야 한다.
    """
    now = now or _now()
    snapshot, delegation = current_snapshot(now), current_delegation(now)
    if snapshot is None or delegation is None:
        raise CatalogError("목록 정보가 없거나 만료됐습니다. 인터넷에 연결해 목록 정보를 받으세요.")
    doc = parse(release_raw)
    preview = doc.get("payload") if isinstance(doc, dict) and isinstance(doc.get("payload"), dict) else {}
    try:
        approved = _time(preview.get("approved_at"))
    except CatalogError:
        raise CatalogError("발행 문서의 발행 시각이 맞지 않습니다") from None
    if approved > now + CLOCK_SKEW:
        raise CatalogError("발행 시각이 미래인 문서입니다")
    # 발행 시각에 위임돼 있던 키여야 한다. 은퇴한 키라도 목록 정보(현재 키 서명)에 실려 있으면 인정된다 — 아래 대조.
    keys = {k["public_key"] for k in delegation["catalog_keys"]
            if k["public_key"] not in set(delegation["compromised_keys"])
            and _time(k["not_before"]) <= approved <= _time(k["not_after"])}
    release = verify(doc, "release", keys)
    rune_id, version = release["rune_id"], release["version"]
    if expect is not None and (rune_id, version) != tuple(expect):
        raise CatalogError("고른 것과 다른 패키지의 발행 문서입니다")
    if release["epoch"] != delegation["epoch"]:
        raise CatalogError("발행 문서의 세대가 지금 위임과 다릅니다")
    if rune_id not in ALLOWED_IDS or release["publisher_account_id"] not in PUBLISHERS:
        raise CatalogError("목록에서 받을 수 있는 룬·발행자가 아닙니다")
    listed = [r for r in snapshot["releases"] if (r["rune_id"], r["version"]) == (rune_id, version)]
    if len(listed) != 1:
        raise CatalogError("목록에 없는 버전입니다")
    for field in ("archive_sha256", "manifest_sha256", "size", "profile", "permissions"):
        if release[field] != listed[0][field]:
            raise CatalogError(f"발행 문서와 목록 정보의 {field} 가 다릅니다")
    if blocked_install(release["archive_sha256"]) or any(
            r["archive_sha256"] == release["archive_sha256"] for r in snapshot["revocations"]):
        raise CatalogError("회수된 버전은 설치하지 않습니다")

    return doc, release, listed[0]


def _linked(path: Path) -> bool:
    """그 항목 **자체**가 심볼릭 링크·정션(재분석 지점)인가 — 상위 폴더는 보지 않는다 (codex1 회신 3·4).
    `is_junction` 은 3.12 부터라 Windows 속성 비트로 본다."""
    import stat
    info = os.lstat(path)
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def _same_recording(mine: Path, theirs: Path) -> bool:
    """이 PC 의 녹화가 패키지의 녹화와 같은가 — 발행은 녹화를 정리해 다시 쓰므로 같은 규칙으로 정리해 비교한다."""
    from vader.core import release
    if not theirs.is_file():
        return False
    try:
        found = json.loads(theirs.read_text(encoding="utf-8"))
        return found in (json.loads(mine.read_text(encoding="utf-8")), json.loads(release._dump(release._clean(mine))))
    except Exception:  # noqa: BLE001 — 정리할 수 없는 녹화는 같다고 볼 수 없다
        return False


def install_catalog(archive: Path, release_raw: bytes, now: datetime | None = None,
                    rune_dir: Path | None = None, replace_local: bool = False):
    """목록에서 받은 zip 을 검증해 설치한다. 실패하면 일반 설치로 넘어가지 않는다.

    설치는 **실전 승인이 아니다** — 그 PC 에서 연습한 뒤 사람이 봉인해야 실전이 된다 (기존 `seal`).
    """
    import shutil
    import tempfile
    import zipfile

    from vader.core import installer, profile, seal

    now = now or _now()
    doc, release, _listed = check_release(release_raw, now)
    rune_id, version = release["rune_id"], release["version"]
    # 이 PC 에서 직접 설치한 같은 id 를 목록 룬으로 조용히 바꾸지 않는다 — 지운 뒤에 받는다 (마스터 2026-10-30)
    # ponytail: 검사와 교체 사이는 화면의 controller.lock 으로만 직렬화된다. 다른 프로세스가 같은 룬 폴더에
    # 설치하는 경로가 생기면 설치·목록 설치·제거가 공유하는 파일 잠금으로 올린다 (codex1 6단계 P2-3)
    # replace_local — 사람이 확인 창에서 '직접 설치한 룬을 광장 버전으로 바꾸기' 를 고른 경우만 (마스터 2026-11-26)
    local = local_rune(rune_id, rune_dir)
    if local and not replace_local:
        raise CatalogError("이 PC 에 직접 설치한 같은 룬이 있습니다. 내 룬에서 제거한 뒤 광장에서 받으세요.")

    previous = origins().get(rune_id)
    target = (rune_dir or config.rune_dir()) / rune_id
    kept = None
    with tempfile.TemporaryDirectory(prefix="vader-catalog-") as tmp:
        fixed = Path(tmp) / "package.zip"               # 바꿀 수 없는 사본 — 끝까지 이것만 쓴다
        shutil.copyfile(archive, fixed)
        installer.check_archive(fixed)
        data = fixed.read_bytes()
        with zipfile.ZipFile(fixed) as book:
            manifest_bytes = book.read("manifest.json")
        manifest = parse(manifest_bytes)
        if (hashlib.sha256(data).hexdigest() != release["archive_sha256"]
                or hashlib.sha256(manifest_bytes).hexdigest() != release["manifest_sha256"]
                or len(data) != release["size"]):
            raise CatalogError("받은 파일이 발행 문서와 다릅니다")
        if not isinstance(manifest, dict) or not manifest.get("fingerprint") or (
                manifest.get("id"), manifest.get("version"), manifest.get("profile"), sorted(manifest.get("permissions") or [])
        ) != (rune_id, version, release["profile"], sorted(release["permissions"])):
            raise CatalogError("패키지 manifest 가 발행 문서와 다릅니다")
        source = installer._unpack(fixed, Path(tmp) / "unpacked")
        if local:
            # 링크·정션(룬 폴더 자신 포함)이 있으면 폴더 밖 자료를 끌어올 수 있다 — 무엇이든 읽기 전에 거절 (회신 2 P2-2, 회신 3)
            if _linked(target) or any(_linked(Path(top, n)) for top, dirs, files in os.walk(target) for n in dirs + files):
                raise CatalogError("룬 폴더에 바로 가기 링크가 있어 바꾸지 않았어요.")
            # 룬 폴더 안의 녹화는 바꾸면 사라진다 — 패키지에 같은 녹화(발행 때처럼 정리한 것)가 없으면 바꾸지 않는다
            mine = target / "recordings"
            if any(not _same_recording(p, source / "recordings" / p.name) for p in (mine.glob("*.json") if mine.is_dir() else ())):
                raise CatalogError("이 PC 에서 고친 녹화가 있어 광장 버전으로 바꾸지 않았어요. 녹화를 옮겨 둔 뒤 다시 하세요.")
            # 화면이 본 값이 아니라 지금 디스크의 버전으로 — 그사이 더 새 판을 직접 깔았으면 덮지 않는다 (codex1 회신 P2-3)
            have = _version(installer._installed_version(target))     # 문자열 리터럴로 읽히는 판만 — 모르면 바꾸지 않는다 (회신 2 P2-1)
            if not have:
                raise CatalogError("이 PC 의 룬 버전을 읽지 못해 바꾸지 않았어요.")
            if _version(version) <= have:
                raise CatalogError("이 PC 의 룬이 광장 버전과 같거나 더 새것이라 바꾸지 않았어요.")
            # 룬 폴더 안의 다른 파일까지 — 지금 폴더를 룬 경로 밖에 통째로 보관한다 (codex1 회신 P1-2)
            kept = config.data_dir() / "replaced-runes" / f"{rune_id}-{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}"
            try:
                shutil.copytree(target, kept, symlinks=True, ignore=shutil.ignore_patterns("__pycache__"))
            except Exception:
                shutil.rmtree(kept, ignore_errors=True)
                raise
        # import 전에 소스의 선언을 AST 로 본다
        declared = (installer._manifest_string(source, "id"), installer._manifest_string(source, "version"),
                    sorted(installer._declared(source)), profile.declared(source) or "legacy")
        if declared != (rune_id, version, sorted(release["permissions"]), release["profile"]):
            raise CatalogError("소스의 선언이 발행 문서와 다릅니다")

        entry = {"state": "pending", "archive_sha256": release["archive_sha256"], "public_key": doc["public_key"],
                 "version": version, "fingerprint": manifest["fingerprint"], "release": doc,
                 **({"replaced_local": str(kept)} if kept else {})}
        before = seal.fingerprint(target)
        record_origin(rune_id, entry)                   # 먼저 '설치 중' — 중간에 꺼지면 판정표가 막는다
        try:
            done = installer.install(source, rune_dir, merge_recordings=False, catalog_install=True)
        except Exception:
            # 폴더가 정말 예전 그대로일 때만 원장을 되돌린다. 새 파일이 남았으면 pending 으로 잠근다 (codex1 회신 P1-1)
            if seal.fingerprint(target) == before:
                _restore_origin(rune_id, previous)
            raise
        actual = (done.manifest.id, done.manifest.version, sorted(done.manifest.permissions))
        if seal.fingerprint(done.path) != manifest["fingerprint"] or actual != (
                rune_id, version, sorted(release["permissions"])):
            # ponytail: 파일은 이미 새 것 — 원장을 pending 으로 남겨 판정표가 막는다. 자동 되돌리기는 설치기 분리 때.
            raise CatalogError("설치된 룬이 서명된 패키지와 다릅니다. 다시 받으세요.")
    record_origin(rune_id, {**entry, "state": "installed"})
    return done
