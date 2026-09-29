"""Download the pinned FastEmbed models into ``DOCRAG_MODEL_DIR`` (image build step).

Each model is fetched at its EXACT pinned commit (an immutable revision) into its own
folder — ``<model_dir>/dense`` and ``<model_dir>/sparse`` — which is what the runtime
loads (``specific_model_path``). An upstream update therefore cannot change the baked
vectors, nor break the build; bumping a pin is a deliberate change that also moves the
index to a new collection, rebuilt from the saved chunks.

    DOCRAG_MODEL_DIR=/opt/models python -m app.rag.bake_models
"""

from __future__ import annotations

import time
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


def download(fetch: Any, source: str, revision: str, local_dir: str,
             attempts: int = 4, pause_s: float = 5.0) -> str:
    """snapshot_download, retried: Hugging Face occasionally drops one of the many
    small files a model repo holds (a 499 or a reset), and that one file must not
    fail a whole image build. Files already fetched are kept between attempts; few
    parallel requests keep the burst under the hub's limits."""
    for attempt in range(1, attempts + 1):
        try:
            return str(fetch(source, revision=revision, local_dir=local_dir, max_workers=2))
        except Exception as exc:  # noqa: BLE001 - any transport/hub error is retried
            if attempt == attempts:
                raise
            print(f"download of {source} failed ({type(exc).__name__}); "
                  f"retry {attempt}/{attempts - 1} in {pause_s * attempt:.0f}s")
            time.sleep(pause_s * attempt)
    raise AssertionError("unreachable")


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
        path = download(snapshot_download, source, pinned, str(Path(s.model_dir) / kind))
        model = cls(name, specific_model_path=path)
        list(model.embed(["docrag model bake"]))
        print(f"baked {kind} {name} @ {pinned[:12]} from {source}")


if __name__ == "__main__":
    main()
