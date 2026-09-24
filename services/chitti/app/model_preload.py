"""One-time online FastEmbed model preload with an exact revision manifest."""

from __future__ import annotations

import argparse
import json
from importlib.metadata import version
from pathlib import Path

from huggingface_hub import model_info

from app.config import get_settings
from app.semantic import MANIFEST_FILE, expected_manifest, load_models, verify_model_manifest


def _source_repo(model_class, model_name: str) -> str:  # noqa: ANN001
    match = next(
        (item for item in model_class.list_supported_models() if item.get("model") == model_name),
        None,
    )
    source = ((match or {}).get("sources") or {}).get("hf")
    if not source:
        raise RuntimeError(f"FastEmbed model '{model_name}' has no Hugging Face source.")
    return str(source)


def main(*, reuse_existing: bool = False) -> None:
    from fastembed import SparseTextEmbedding, TextEmbedding
    from fastembed.rerank.cross_encoder import TextCrossEncoder

    settings = get_settings()
    if reuse_existing and (Path(settings.model_cache_dir) / MANIFEST_FILE).exists():
        verify_model_manifest(settings)
        settings.model_offline = True
        load_models(settings)
        print("Verified existing pinned Chitti model cache offline.")
        return
    configured = expected_manifest(settings)
    model_types = {
        "dense": TextEmbedding,
        "sparse": SparseTextEmbedding,
        "reranker": TextCrossEncoder,
    }
    for key, model_type in model_types.items():
        source = _source_repo(model_type, configured[key]["model"])
        actual_revision = model_info(source).sha
        if actual_revision != configured[key]["revision"]:
            raise RuntimeError(
                f"Configured {key} revision {configured[key]['revision']} does not match "
                f"current source revision {actual_revision} for {source}."
            )
    settings.model_offline = False
    models = load_models(settings)
    list(models.dense.embed(["Chitti model preload"]))
    list(models.sparse.embed(["Chitti model preload"]))
    list(models.reranker.rerank("Chitti", ["Chitti model preload"]))
    manifest = {
        "fastembed_version": version("fastembed"),
        "models": configured,
    }
    path = Path(settings.model_cache_dir) / MANIFEST_FILE
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(manifest, indent=2) + "\n")
    temporary.replace(path)
    print(json.dumps(manifest))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reuse-existing", action="store_true",
                        help="Verify an existing cache offline; never replace its manifest.")
    main(reuse_existing=parser.parse_args().reuse_existing)
