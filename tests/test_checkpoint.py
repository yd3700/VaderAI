import json
import os

import pytest

from vader.core.checkpoint import CheckpointStore
from vader.sdk import CHECKPOINT_SCHEMA_VERSION, CheckpointKey


def _key(rune_id: str = "sample", identity: str = "a" * 64) -> CheckpointKey:
    return CheckpointKey(rune_id=rune_id, input_identity=identity)


def test_save_load_round_trip(tmp_path):
    store = CheckpointStore(tmp_path)
    store.save(_key(), {"step": 3, "name": "입고"})

    assert store.load(_key()) == {"step": 3, "name": "입고"}


def test_missing_checkpoint_returns_none(tmp_path):
    assert CheckpointStore(tmp_path).load(_key()) is None


def test_clear_removes_checkpoint_and_is_idempotent(tmp_path):
    store = CheckpointStore(tmp_path)
    store.save(_key(), {"step": 1})

    store.clear(_key())
    store.clear(_key())

    assert store.load(_key()) is None


def test_different_schema_version_returns_none(tmp_path):
    store = CheckpointStore(tmp_path)
    store.save(_key(), {"step": 1})
    path = next(tmp_path.glob("*.json"))
    path.write_text(
        json.dumps(
            {
                "schema_version": CHECKPOINT_SCHEMA_VERSION + 1,
                "data": {"step": 1},
            }
        ),
        encoding="utf-8",
    )

    assert store.load(_key()) is None


def test_broken_json_returns_none(tmp_path):
    store = CheckpointStore(tmp_path)
    store.save(_key(), {"step": 1})
    next(tmp_path.glob("*.json")).write_text("{broken", encoding="utf-8")

    assert store.load(_key()) is None


def test_failed_replace_preserves_previous_checkpoint(tmp_path, monkeypatch):
    store = CheckpointStore(tmp_path)
    store.save(_key(), {"step": 1})

    def fail_replace(source, destination):
        raise OSError("simulated power failure")

    monkeypatch.setattr(os, "replace", fail_replace)

    with pytest.raises(OSError, match="power failure"):
        store.save(_key(), {"step": 2})

    assert store.load(_key()) == {"step": 1}
    assert list(tmp_path.glob("*.tmp")) == []


def test_different_input_identities_have_separate_checkpoints(tmp_path):
    store = CheckpointStore(tmp_path)
    first = _key(identity="a" * 64)
    second = _key(identity="b" * 64)

    store.save(first, {"value": "first"})
    store.save(second, {"value": "second"})

    assert store.load(first) == {"value": "first"}
    assert store.load(second) == {"value": "second"}


def test_same_rune_id_does_not_mix_different_inputs(tmp_path):
    store = CheckpointStore(tmp_path)
    original = _key(rune_id="same", identity="1" * 64)
    changed = _key(rune_id="same", identity="2" * 64)

    store.save(original, {"input": "original"})

    assert store.load(changed) is None
    assert store.load(original) == {"input": "original"}


def test_unsafe_rune_id_cannot_escape_store_root(tmp_path):
    store = CheckpointStore(tmp_path)
    key = _key(rune_id="../../CON:병원")

    store.save(key, {"safe": True})

    files = list(tmp_path.glob("*.json"))
    assert len(files) == 1
    assert files[0].parent == tmp_path
    assert store.load(key) == {"safe": True}

