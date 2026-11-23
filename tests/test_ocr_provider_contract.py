"""OCR 제공자라면 구현에 관계없이 지켜야 하는 계약."""
from __future__ import annotations

import inspect
import socket
from pathlib import Path

import pytest

from vader.core.ocr.base import FakeOcrProvider, OcrProvider
from vader.core.ocr import gateway as gateway_module
from vader.core.ocr.gateway import OcrGateway
from vader.core.ocr.claude import ClaudeProvider
from vader.core.ocr.gemini import GeminiProvider
from vader.core.usage import UsageLedger


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("network access is forbidden in OCR tests")

    monkeypatch.setattr(socket, "socket", forbidden)


def providers():
    """새 제공자를 추가할 때 이 등록표에도 넣어 같은 계약을 적용한다."""
    return [ClaudeProvider(), GeminiProvider(), FakeOcrProvider([])]


@pytest.mark.parametrize("provider", providers(), ids=lambda item: item.name())
def test_provider_satisfies_the_runtime_contract(provider):
    assert isinstance(provider, OcrProvider)
    assert isinstance(provider.is_available(), bool)


@pytest.mark.parametrize("provider", providers(), ids=lambda item: item.name())
def test_provider_cache_identity_is_stable_and_explicit(provider):
    for value in (provider.name(), provider.model(), provider.adapter_version()):
        assert isinstance(value, str)
        assert value.strip()
        assert value == value.strip()
    assert provider.name() == provider.name()
    assert provider.model() == provider.model()
    assert provider.adapter_version() == provider.adapter_version()


@pytest.mark.parametrize("provider", providers(), ids=lambda item: item.name())
def test_provider_call_signatures_match_the_contract(provider):
    extract = list(inspect.signature(provider.extract).parameters)
    complete = list(inspect.signature(provider.complete).parameters)
    assert extract == ["path", "prompt", "schema"]
    assert complete == ["prompt", "schema"]


def _cache_paths(tmp_path: Path, provider: FakeOcrProvider) -> tuple[Path, Path]:
    gateway = OcrGateway(
        provider,
        cache_dir=tmp_path,
        cache_days=30,
        request_delay_seconds=0,
        retry_max=1,
        daily_limit=0,
    )
    source = tmp_path / "input.pdf"
    source.write_bytes(b"same document")
    return (
        gateway._cache_path(source, "same prompt", dict),
        gateway._prompt_cache_path("same prompt", dict),
    )


@pytest.mark.parametrize("changed", ["provider", "model", "adapter"])
def test_each_provider_identity_component_invalidates_all_caches(tmp_path, changed):
    baseline = FakeOcrProvider(
        [], provider_name="provider-a", model_name="model-a", adapter_version="1"
    )
    values = {
        "provider": ("provider-b", "model-a", "1"),
        "model": ("provider-a", "model-b", "1"),
        "adapter": ("provider-a", "model-a", "2"),
    }
    provider_name, model_name, adapter_version = values[changed]
    mutated = FakeOcrProvider(
        [],
        provider_name=provider_name,
        model_name=model_name,
        adapter_version=adapter_version,
    )

    before = _cache_paths(tmp_path, baseline)
    after = _cache_paths(tmp_path, mutated)

    assert before[0] != after[0]
    assert before[1] != after[1]


def test_cache_format_version_invalidates_all_caches(tmp_path, monkeypatch):
    provider = FakeOcrProvider([])
    before = _cache_paths(tmp_path, provider)

    monkeypatch.setattr(gateway_module, "CACHE_FORMAT_VERSION", "next")
    after = _cache_paths(tmp_path, provider)

    assert before[0] != after[0]
    assert before[1] != after[1]


def test_gateway_records_the_models_actual_pinned_identity(tmp_path):
    provider = FakeOcrProvider([{"ok": True}], model_name="pinned-model")
    gateway = OcrGateway(
        provider,
        cache_dir=tmp_path / "cache",
        cache_days=30,
        request_delay_seconds=0,
        retry_max=1,
        daily_limit=0,
        ledger=UsageLedger(tmp_path / "usage"),
    )
    source = tmp_path / "input.pdf"
    source.write_bytes(b"document")

    gateway.extract(source, "prompt", dict)

    assert gateway.ledger.read()[0].model == "pinned-model"
