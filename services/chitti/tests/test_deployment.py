"""Dependency readiness and non-destructive deployment preparation."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from app import main, model_preload, semantic_index
from app.semantic import SemanticDependencyError, expected_manifest


@pytest.mark.parametrize("dependency", [None, "register", "qdrant"])
async def test_readiness_tracks_live_dependencies_without_failing_liveness(monkeypatch, dependency):
    app = main.create_app()
    settings = main.get_settings()
    settings.pipeline_enabled = True
    settings.register_base_url = "https://services.example.test:8443/machine"
    app.state.pipeline = object()
    app.state.ontology_index = {}
    qdrant = AsyncMock()
    app.state.retriever = SimpleNamespace(client=qdrant)
    register = AsyncMock()
    register.get.return_value = httpx.Response(200, request=httpx.Request("GET", "http://register/readyz"))
    if dependency == "qdrant":
        qdrant.get_collection.side_effect = httpx.ConnectError("unavailable")
    if dependency == "register":
        register.get.side_effect = httpx.ConnectError("unavailable")
    context = AsyncMock()
    context.__aenter__.return_value = register
    monkeypatch.setattr(main.httpx, "AsyncClient", lambda **kwargs: context)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/readyz")
        assert response.status_code == (503 if dependency else 200)
        assert (await client.get("/healthz")).status_code == 200
        qdrant.get_collection.side_effect = None
        register.get.side_effect = None
        assert (await client.get("/readyz")).status_code == 200
        register.get.assert_awaited_with("https://services.example.test:8443/machine/readyz")


@pytest.mark.parametrize("valid", [True, False])
async def test_reused_index_is_verified_and_never_deleted(monkeypatch, valid):
    models = SimpleNamespace(
        dense=SimpleNamespace(passage_embed=lambda texts: []),
        sparse=SimpleNamespace(passage_embed=lambda texts: []),
    )
    client = AsyncMock()
    client.collection_exists.return_value = True
    verify = AsyncMock(side_effect=None if valid else RuntimeError("index identity mismatch"))
    monkeypatch.setattr(semantic_index, "load_models", lambda settings: models)
    monkeypatch.setattr(semantic_index, "AsyncQdrantClient", lambda **kwargs: client)
    monkeypatch.setattr(semantic_index, "verify_index_integrity", verify)
    if valid:
        await semantic_index.build(reuse_existing=True)
    else:
        with pytest.raises(RuntimeError, match="identity mismatch"):
            await semantic_index.build(reuse_existing=True)
    verify.assert_awaited_once()
    client.delete_collection.assert_not_called()
    client.create_collection.assert_not_called()
    client.upsert.assert_not_called()
    client.close.assert_awaited_once()


@pytest.mark.parametrize("valid", [True, False])
def test_cached_model_preparation_is_offline_and_preserves_manifest(monkeypatch, tmp_path, valid):
    settings = model_preload.get_settings()
    settings.model_cache_dir = str(tmp_path)
    settings.model_offline = False
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"models": expected_manifest(settings) if valid else {}}))
    before = manifest.read_bytes()
    load = Mock()
    online = Mock(side_effect=AssertionError("must not contact model source"))
    monkeypatch.setattr(model_preload, "load_models", load)
    monkeypatch.setattr(model_preload, "model_info", online)
    if valid:
        model_preload.main(reuse_existing=True)
        load.assert_called_once_with(settings)
        assert settings.model_offline is True
    else:
        with pytest.raises(SemanticDependencyError):
            model_preload.main(reuse_existing=True)
        load.assert_not_called()
    online.assert_not_called()
    assert manifest.read_bytes() == before
