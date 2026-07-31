import pytest

from app.config import get_settings


@pytest.fixture(autouse=True)
def _stub_engine(monkeypatch):
    """Every test runs against the stub engine — no model, no faster-whisper import."""
    monkeypatch.setenv("STT_STUB_TEXT", "Met the EcoSoch Solar team about the term loan.")
    monkeypatch.delenv("STT_API_KEYS", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
