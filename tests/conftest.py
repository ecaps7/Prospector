"""Offline by default; no test silently inherits a developer's services or secrets."""

from __future__ import annotations

import socket

import pytest

from prospector.config import Settings, clear_settings_cache
from prospector.store.checkpoint import close_pool
from prospector.store.database import clear_engine_cache


def pytest_addoption(parser):
    parser.addoption("--live", action="store_true", help="Allow paid external-service tests")


def pytest_collection_modifyitems(config, items):
    if config.getoption("--live"):
        return
    selected, deselected = [], []
    for item in items:
        (deselected if item.get_closest_marker("live") else selected).append(item)
    items[:] = selected
    config.hook.pytest_deselected(items=deselected)


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch, request):
    if request.node.get_closest_marker("live"):
        yield
        return
    close_pool()
    clear_engine_cache()
    clear_settings_cache()
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    for key, value in {
        "DATABASE_URL": "postgresql://unused:unused@127.0.0.1:1/unused",
        "S3_ENDPOINT": "http://127.0.0.1:1",
        "S3_ACCESS_KEY": "test-only",
        "S3_SECRET_KEY": "test-only",
        "S3_BUCKET": "test-only",
        "PROSPECTOR_LLM_BASE_URL": "http://llm.test/v1",
        "PROSPECTOR_LLM_API_KEY": "test-only",
        "PROSPECTOR_LLM_MODEL_STRONG": "qwen-test",
        "PROSPECTOR_LLM_MODEL_MID": "qwen-test",
        "EXA_API_KEY": "test-only",
    }.items():
        monkeypatch.setenv(key, value)
    connect = socket.socket.connect

    def local_only(sock, address):
        if isinstance(address, tuple) and address[0] not in {"127.0.0.1", "::1"}:
            raise AssertionError(f"Non-live test attempted network access: {address[0]}")
        return connect(sock, address)

    monkeypatch.setattr(socket.socket, "connect", local_only)
    yield
    close_pool()
    clear_engine_cache()
    clear_settings_cache()
