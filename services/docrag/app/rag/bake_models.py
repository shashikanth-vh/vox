"""Download the pinned FastEmbed models into ``DOCRAG_MODEL_DIR`` (image build step).

Each model is fetched at its EXACT pinned commit (an immutable revision) into its own
folder — ``<model_dir>/dense`` and ``<model_dir>/sparse`` — which is what the runtime
loads (``specific_model_path``). An upstream update therefore cannot change the baked
vectors, nor break the build; bumping a pin is a deliberate change that also moves the
index to a new collection, rebuilt from the saved chunks.

    DOCRAG_MODEL_DIR=/opt/models python -m app.rag.bake_models
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.config import get_settings


def model_source(model_cls: Any, name: str) -> str:
    """The Hugging Face repo FastEmbed serves ``name`` from."""
    entry = next((m for m in model_cls.list_supported_models() if m.get("model") == name), None)
    source = ((entry or {}).get("sources") or {}).get("hf")
    if not source:
        raise SystemExit(f"FastEmbed has no Hugging Face source for {name!r}.")
    return str(source)


def main() -> None:
    from fastembed import SparseTextEmbedding, TextEmbedding
    from huggingface_hub import snapshot_download

    s = get_settings()
    if not s.model_dir:
        raise SystemExit("DOCRAG_MODEL_DIR must be set to bake models.")
    for kind, cls, name, pinned in (
            ("dense", TextEmbedding, s.dense_model, s.dense_model_revision),
            ("sparse", SparseTextEmbedding, s.sparse_model, s.sparse_model_revision)):
        source = model_source(cls, name)
        path = snapshot_download(source, revision=pinned, local_dir=str(Path(s.model_dir) / kind))
        model = cls(name, specific_model_path=path)
        list(model.embed(["docrag model bake"]))
        print(f"baked {kind} {name} @ {pinned[:12]} from {source}")


if __name__ == "__main__":
    main()
