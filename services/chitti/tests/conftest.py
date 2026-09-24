from __future__ import annotations

import os

import pytest

from app.config import get_settings

for _name, _value in {
    "CHITTI_DENSE_CANDIDATES": "40",
    "CHITTI_SPARSE_CANDIDATES": "40",
    "CHITTI_FUSED_CANDIDATES": "40",
    "CHITTI_GROUNDING_RERANK_LIMIT": "20",
    "CHITTI_PLANNING_RERANK_LIMIT": "12",
}.items():
    os.environ.setdefault(_name, _value)


@pytest.fixture(autouse=True)
def _settings(monkeypatch):
    monkeypatch.setenv("CHITTI_API_KEYS", "test-client-key,gateway-service-key")
    monkeypatch.setenv("CHITTI_PUBLIC_MODEL_ID", "prism-chitti")
    monkeypatch.setenv("CHITTI_LOG_JSON", "false")
    monkeypatch.setenv("CHITTI_DENSE_CANDIDATES", "40")
    monkeypatch.setenv("CHITTI_SPARSE_CANDIDATES", "40")
    monkeypatch.setenv("CHITTI_FUSED_CANDIDATES", "40")
    monkeypatch.setenv("CHITTI_GROUNDING_RERANK_LIMIT", "20")
    monkeypatch.setenv("CHITTI_PLANNING_RERANK_LIMIT", "12")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
