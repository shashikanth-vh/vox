"""Embedding engines — dense (semantic) + sparse (BM25 keyword) vectors per text.

The real engine runs FastEmbed (ONNX on CPU, no torch). Both models are pinned by
revision and baked into the image; with ``model_offline`` the container serves only what
was baked, so a deploy never depends on — or silently drifts with — huggingface.co.
The stub hashes tokens into both vector kinds: deterministic, model-free, lexical enough
for tests to assert on ranking.
"""

from __future__ import annotations

import hashlib
import re
import threading
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import numpy as np

from app.config import Settings


class ModelUnavailableError(RuntimeError):
    """The embedding models could not be loaded — a deployment fault, not a bad request."""


@dataclass(frozen=True, slots=True)
class Sparse:
    indices: list[int]
    values: list[float]


class Embedder(Protocol):
    dim: int
    load_error: str

    @property
    def loaded(self) -> bool: ...

    def warm(self) -> None: ...

    def embed_passages(self, texts: list[str]) -> tuple[np.ndarray, list[Sparse]]: ...

    def embed_query(self, text: str) -> tuple[np.ndarray, Sparse]: ...


# The real sparse model (Qdrant/bm25) drops English stopwords; the stub mirrors that so
# "what is the tenor" is matched on "tenor", not on "the".
_STOPWORDS = frozenset(
    "a an and are as at be by for from has have in is it of on or that the this to was "
    "what when where which who with".split())


def _hash(token: str) -> int:
    return int.from_bytes(hashlib.blake2b(token.encode(), digest_size=4).digest(), "big")


class StubEmbedder:
    """Feature-hashed bag of words for both vectors. Tests and CI only."""

    load_error = ""
    loaded = True

    def __init__(self, dim: int = 384) -> None:
        self.dim = dim

    def warm(self) -> None:  # pragma: no cover - trivial
        return None

    def _one(self, text: str) -> tuple[np.ndarray, Sparse]:
        tokens = [t for t in re.findall(r"\w+", text.lower()) if t not in _STOPWORDS]
        dense = np.zeros(self.dim, dtype=np.float32)
        for tok in tokens:
            dense[_hash(tok) % self.dim] += 1.0
        norm = np.linalg.norm(dense)
        if norm:
            dense /= norm
        counts = Counter(_hash(tok) % 1_000_003 for tok in tokens)
        return dense, Sparse(indices=list(counts), values=[float(v) for v in counts.values()])

    def embed_passages(self, texts: list[str]) -> tuple[np.ndarray, list[Sparse]]:
        pairs = [self._one(t) for t in texts]
        dense = np.stack([d for d, _ in pairs]) if pairs else np.zeros((0, self.dim), np.float32)
        return dense, [s for _, s in pairs]

    def embed_query(self, text: str) -> tuple[np.ndarray, Sparse]:
        return self._one(text)


class FastEmbedEmbedder:
    def __init__(self, settings: Settings):
        self.s = settings
        self.dim = settings.dense_dimensions
        self._dense: Any = None
        self._sparse: Any = None
        self._lock = threading.Lock()
        self.load_error = ""

    @property
    def loaded(self) -> bool:
        return self._dense is not None and self._sparse is not None

    def warm(self) -> None:
        with self._lock:
            self._load()

    def _load(self) -> None:
        if self.loaded:
            return
        try:
            from fastembed import SparseTextEmbedding, TextEmbedding  # heavy; lazy

            if self.s.model_offline:
                # Serve exactly what the image baked: <model_dir>/{dense,sparse}, each the
                # pinned revision (see bake_models). Nothing is looked up or downloaded.
                base = Path(self.s.model_dir)
                self._dense = TextEmbedding(self.s.dense_model,
                                            specific_model_path=str(base / "dense"))
                self._sparse = SparseTextEmbedding(self.s.sparse_model,
                                                   specific_model_path=str(base / "sparse"))
            else:
                cache = self.s.model_dir or None
                self._dense = TextEmbedding(self.s.dense_model, cache_dir=cache)
                self._sparse = SparseTextEmbedding(self.s.sparse_model, cache_dir=cache)
        except Exception as exc:  # noqa: BLE001 - re-raised, typed
            self._dense = self._sparse = None
            self.load_error = (
                f"embedding models {self.s.dense_model!r} / {self.s.sparse_model!r} could not "
                f"be loaded from {self.s.model_dir or 'the FastEmbed cache'!r}: "
                f"{type(exc).__name__}: {exc}. The image bakes the pinned models and serves "
                "offline, so DOCRAG_DENSE_MODEL / DOCRAG_SPARSE_MODEL must match the build.")
            raise ModelUnavailableError(self.load_error) from exc
        self.load_error = ""

    def embed_passages(self, texts: list[str]) -> tuple[np.ndarray, list[Sparse]]:
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32), []
        with self._lock:
            self._load()
            dense = np.asarray(list(self._dense.passage_embed(texts)), dtype=np.float32)
            sparse = [Sparse(indices=s.indices.tolist(), values=s.values.tolist())
                      for s in self._sparse.passage_embed(texts)]
        if dense.shape[1] != self.dim:
            raise ModelUnavailableError(
                f"dense model returned {dense.shape[1]} dimensions; configured {self.dim}")
        return dense, sparse

    def embed_query(self, text: str) -> tuple[np.ndarray, Sparse]:
        with self._lock:
            self._load()
            dense = np.asarray(next(iter(self._dense.query_embed(text))), dtype=np.float32)
            s = next(iter(self._sparse.query_embed(text)))
        return dense, Sparse(indices=s.indices.tolist(), values=s.values.tolist())


def build_embedder(settings: Settings) -> Embedder:
    if settings.embedder == "stub":
        return StubEmbedder(settings.dense_dimensions)
    return FastEmbedEmbedder(settings)
