"""메신저 사진 — 다시 만들기·암호화 (2026-11-25 마스터 요청, codex1 설계 회신).

원본 바이트는 이 모듈 밖(화면·서버)으로 나가지 않는다. 보내는 쪽도 받는 쪽도 같은 규칙으로 **다시 만든** JPEG 만 쓴다:
JPEG/PNG 한 장(애니메이션·다른 형식 거부) → 픽셀을 읽기 전에 가로·세로·총 픽셀 확인 → EXIF 회전 → 투명은 흰 바탕에 →
긴 변 2048 → 메타데이터 없이 JPEG. 위치·기기 정보 같은 EXIF 는 따라가지 않는다. 사진 속 글자·얼굴은 그대로다 — 화면이 안내한다.
서버에는 사진마다 새 키(AES-256-GCM)로 잠근 암호문만 간다. 키는 메신저 본문(Olm) 안에만.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import os

MARK = "⁣vader-image:"          # 메신저 글 앞머리 — 화면은 이걸 보고 말풍선을 사진으로 그린다 (할 일 공유와 같은 방식)
RAW_MAX = 20 * 1024 * 1024           # 사람이 고른 원본
SIDE = 2048                          # 다시 만든 사진의 긴 변
PIXELS = 40_000_000                  # 원본 총 픽셀 상한 — 넘으면 디코딩하지 않는다
CIPHER_MAX = 1572864                 # 서버 상한(장당 암호문)과 같다
JPEG_MAX = CIPHER_MAX - 16           # GCM 태그 16바이트를 뺀 JPEG 예산
_AAD = b"vader-image-v1"
# ponytail: 40M 픽셀·2048 은 문서값. 최소 사양 PC 에서 메모리를 재 보고 줄일 수 있다 (codex1 회신 3).


class ImageRefused(ValueError):
    pass


def clean(raw: bytes, *, received: bool = False) -> tuple[bytes, int, int]:
    """다시 만든 JPEG 와 크기. 받은 사진은 JPEG·긴 변 2048 이하·한 장이어야 한다(보내는 쪽 규칙을 다시 확인)."""
    from PIL import Image, ImageOps
    if not raw or len(raw) > RAW_MAX:
        raise ImageRefused("사진은 20MB 까지 보낼 수 있어요.")
    try:
        im = Image.open(io.BytesIO(raw), formats=("JPEG",) if received else ("JPEG", "PNG"))
    except Exception:  # noqa: BLE001 — 형식을 모르면 거절
        raise ImageRefused("JPG·PNG 사진만 보낼 수 있어요.") from None
    with im:
        w, h = im.size
        if w < 1 or h < 1 or w * h > PIXELS or max(w, h) > 20000 or (received and max(w, h) > SIDE):
            raise ImageRefused("사진이 너무 커요.")
        if getattr(im, "n_frames", 1) != 1 or getattr(im, "is_animated", False):
            raise ImageRefused("움직이는 사진은 보낼 수 없어요.")
        if im.format == "JPEG":
            im.draft("RGB", (SIDE, SIDE))                 # 큰 JPEG 은 줄여서 읽는다 — 메모리
        try:
            pic = ImageOps.exif_transpose(im)
            if pic.mode in ("RGBA", "LA", "PA") or (pic.mode == "P" and "transparency" in pic.info):
                pic = pic.convert("RGBA")
                back = Image.new("RGB", pic.size, (255, 255, 255))
                back.paste(pic, mask=pic.getchannel("A"))
                pic = back
            pic = pic.convert("RGB")
            pic.thumbnail((SIDE, SIDE))
            # 픽셀만 새 이미지로 — 원본의 JPEG 주석(COM)·EXIF·ICC 같은 정보가 따라오지 않게 (codex1 사진 회신 2 P1-1)
            pic = Image.frombytes("RGB", pic.size, pic.tobytes())
        except Exception:  # noqa: BLE001 — 깨진 데이터
            raise ImageRefused("사진을 읽지 못했어요.") from None
    side = max(pic.size)
    while True:
        for quality in (85, 75, 65):
            out = io.BytesIO()
            pic.save(out, "JPEG", quality=quality, optimize=True)   # info·exif 를 넘기지 않는다 — 메타데이터 없음
            if out.tell() <= JPEG_MAX:
                return out.getvalue(), pic.size[0], pic.size[1]
        side = int(side * 0.8)
        if side < 64:
            raise ImageRefused("사진을 줄이지 못했어요.")
        pic.thumbnail((side, side))


def seal(jpeg: bytes) -> tuple[bytes, dict]:
    """(서버로 갈 암호문, 메신저 본문에 넣을 열쇠·확인값)."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    key, nonce = AESGCM.generate_key(bit_length=256), os.urandom(12)
    cipher = AESGCM(key).encrypt(nonce, jpeg, _AAD)
    return cipher, {"key": base64.b64encode(key).decode(), "nonce": base64.b64encode(nonce).decode(),
                    "sha256": hashlib.sha256(jpeg).hexdigest()}


def open_sealed(cipher: bytes, meta: dict) -> bytes:
    """태그·해시를 확인한 JPEG. 아니면 ImageRefused."""
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    if not cipher or len(cipher) > CIPHER_MAX:
        raise ImageRefused("사진 크기가 맞지 않아요.")
    try:
        key = base64.b64decode(meta["key"], validate=True)
        nonce = base64.b64decode(meta["nonce"], validate=True)
        jpeg = AESGCM(key).decrypt(nonce, cipher, _AAD)
    except (InvalidTag, KeyError, ValueError, TypeError):
        raise ImageRefused("사진이 손상됐어요.") from None
    if hashlib.sha256(jpeg).hexdigest() != meta.get("sha256"):
        raise ImageRefused("사진이 손상됐어요.")
    return jpeg


def payload(attachment: str, meta: dict, w: int, h: int) -> str:
    return MARK + json.dumps({"v": 1, "id": attachment, **meta, "w": w, "h": h}, separators=(",", ":"))


def parse(text: str) -> dict | None:
    """메신저 글이 사진이면 그 설명. 형식이 틀리면 None."""
    if not text.startswith(MARK):
        return None
    try:
        d = json.loads(text[len(MARK):])
    except (ValueError, RecursionError):      # 너무 깊게 겹친 JSON 도 그 글 하나에서 끝 (codex1 사진 회신 3 P1-1)
        return None
    # 객체·필드 형식·범위를 먼저 본다 — 잘못된 설명 하나가 받기 묶음을 멈추지 않게 (codex1 사진 회신 2 P1-2)
    if not isinstance(d, dict) or d.get("v") != 1:
        return None
    if not all(isinstance(d.get(k), str) and 0 < len(d[k]) <= 100 for k in ("id", "key", "nonce", "sha256")):
        return None
    w, h = d.get("w"), d.get("h")
    if not all(type(x) is int and 0 < x <= SIDE for x in (w, h)):
        return None
    return {"id": d["id"], "key": d["key"], "nonce": d["nonce"], "sha256": d["sha256"], "w": w, "h": h}
