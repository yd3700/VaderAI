"""Python 과 Node 가 같은 문서를 같은 바이트로 읽고 같은 서명을 인정한다 (신뢰 계약 v2 2절 공통 시험 자료)."""
from tests.private import needs
import base64
import json
import shutil
import subprocess
from pathlib import Path

import pytest

pytest.importorskip("cryptography")
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402

from vader.core import catalog  # noqa: E402
from vader.core.signing import _raw  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
CANONICAL = {
    "정렬과 한글": r'{"b":1,"a":"가나","가":true}',
    "이모지와 BMP 끝 글자 정렬": r'{"😀":1,"￿":2,"z":3}',
    "제어 문자": r'{"t":"줄\n바꿈\u0001끝\"따옴표\\"}',
    "중첩": r'{"x":[{"b":[1,2],"a":{}}],"y":false}',
    "가장 큰 정수": r'{"n":9007199254740991}',
    "너무 큰 정수": r'{"n":9007199254740992}',
    "음수": r'{"n":-1}',
    "음수 영": r'{"n":-0}',
    "소수로 쓴 정수": r'{"n":1.0}',
    "지수": r'{"n":1e3}',
    "null": r'{"n":null}',
    "같은 키 두 번": r'{"a":1,"a":2}',
    "짝 없는 서로게이트": r'{"s":"\ud800"}',
    "뒤에 남은 글자": r'{"a":1} x',
    "__proto__ 키": r'{"__proto__":{"x":1},"a":1}',
    "깊은 곳의 __proto__": r'{"a":[{"__proto__":1}]}',
    "constructor 키는 평범한 키": r'{"constructor":1,"prototype":2}',
}


def python_hex(text):
    try:
        return {"hex": catalog.canonical(catalog.parse(text.encode("utf-8"))).hex()}
    except catalog.CatalogError:
        return {"error": True}


@pytest.mark.skipif(shutil.which("node") is None, reason="node 가 없다")
@needs("web")
def test_python_and_node_agree_byte_for_byte(tmp_path):
    key = Ed25519PrivateKey.generate()
    public = base64.b64encode(_raw(key.public_key())).decode()
    good = catalog.sign("snapshot", {"schema": 1, "purpose": "snapshot", "epoch": 1, "sequence": 1,
                                     "issued_at": "2026-11-02T09:00:00+00:00", "expires_at": "2026-11-09T09:00:00+00:00",
                                     "releases": [], "revocations": [], "메모": "\U0001F600 가나"}, key)
    tampered = {**good, "payload": {**good["payload"], "sequence": 2}}
    text = catalog.canonical(good).decode()
    signed = [{"name": "정상", "purpose": "snapshot", "text": text, "keys": [public]},
              {"name": "바뀐 내용", "purpose": "snapshot", "text": catalog.canonical(tampered).decode(), "keys": [public]},
              {"name": "다른 목적", "purpose": "release", "text": text, "keys": [public]},
              {"name": "모르는 키", "purpose": "snapshot", "text": text, "keys": []},
              # 서명 뒤에 __proto__ 필드를 끼워 넣었다 — Node 가 그 필드를 빼고 읽으면 원래 서명이 맞아 버린다
              {"name": "끼워 넣은 __proto__", "purpose": "snapshot",
               "text": text.replace('"payload":{', '"payload":{"__proto__":{"extra":"changed"},', 1),
               "keys": [public]}]
    assert '"payload":{"__proto__"' in signed[-1]["text"]
    vectors = tmp_path / "vectors.json"
    vectors.write_text(json.dumps({"canonical": [{"name": n, "text": t} for n, t in CANONICAL.items()],
                                   "signed": signed}, ensure_ascii=False), encoding="utf-8")
    done = subprocess.run(["node", str(ROOT / "web" / "scripts" / "check-catalog-vectors.mjs"), str(vectors)],
                          capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert done.returncode == 0, done.stderr
    node = json.loads(done.stdout)
    assert len(node["canonical"]) == len(CANONICAL)
    for row in node["canonical"]:
        mine = python_hex(CANONICAL[row["name"]])
        assert ("error" in row) == ("error" in mine), row["name"]
        assert row.get("hex") == mine.get("hex"), row["name"]
    assert [r["ok"] for r in node["signed"]] == [True, False, False, False, False]
