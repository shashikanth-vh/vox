"""Build the versioned Chitti ontology collection in Qdrant."""

from __future__ import annotations

import argparse
import asyncio
import math
import uuid

from qdrant_client import AsyncQdrantClient
from qdrant_client.models import (
    Distance,
    Modifier,
    PointStruct,
    SparseVector,
    SparseVectorParams,
    VectorParams,
)

from app.config import get_settings
from app.contracts import AUDIENCES, ROLE_INVARIANT, ROLES
from app.semantic import (
    DENSE_VECTOR,
    SPARSE_VECTOR,
    collection_name,
    load_models,
    load_passages,
    ontology_content_identity,
    verify_index_integrity,
)

ALLOWED_SEMANTIC_FACETS = frozenset({
    "book_scope",
    "dimension",
    "execution",
    "governance",
    "lifecycle",
    "metric",
    "relationship",
    "resource",
    "vocabulary",
})
ALLOWED_ROLES = ROLES
ALLOWED_AUDIENCES = AUDIENCES


def passage_metadata(passage: dict) -> dict:
    """Validate and canonicalize explicitly authored retrieval facets."""
    metadata = dict(passage.get("metadata") or {})
    facets = metadata.get("semantic_facets")
    if not isinstance(facets, list) or not facets:
        raise ValueError(f"Passage {passage['id']} must declare semantic_facets.")
    normalized = sorted({str(facet).strip() for facet in facets if str(facet).strip()})
    invalid = set(normalized) - ALLOWED_SEMANTIC_FACETS
    if invalid:
        raise ValueError(
            f"Passage {passage['id']} has unsupported semantic facets: {sorted(invalid)}"
        )
    metadata["semantic_facets"] = normalized
    role = metadata.get("role")
    audience = metadata.get("audience")
    if role not in ALLOWED_ROLES:
        raise ValueError(f"Passage {passage['id']} must declare role semantic or invariant.")
    if not isinstance(audience, list) or not audience or not set(audience) <= ALLOWED_AUDIENCES:
        raise ValueError(f"Passage {passage['id']} must declare valid audience tags.")
    metadata["audience"] = sorted(set(audience))
    return metadata


def passage_retrieval_text(passage: dict) -> str:
    """Create the neutral text embedded and reranked for one governed passage."""
    metadata = passage_metadata(passage)
    topic = str(passage["id"]).replace(".", " ").replace("_", " ")
    descriptors = [
        f"Topic: {topic}.",
        f"Source: {passage['source']}.",
        f"Semantic facets: {', '.join(metadata['semantic_facets'])}.",
    ]
    if resource := metadata.get("resource"):
        descriptors.append(f"Register resource: {resource}.")
    if field := metadata.get("field"):
        descriptors.append(f"Governed field: {field}.")
    descriptors.append(str(passage["content"]))
    return " ".join(descriptors)


async def build(*, reuse_existing: bool = False) -> None:
    settings = get_settings()
    source = load_passages()
    passages = [
        passage for passage in source["passages"]
        if (passage.get("metadata") or {}).get("role") != ROLE_INVARIANT
    ]
    texts = [passage_retrieval_text(passage) for passage in passages]
    client = AsyncQdrantClient(
        url=settings.qdrant_url,
        api_key=settings.qdrant_api_key or None,
        timeout=math.ceil(settings.register_timeout_seconds),
    )
    name = collection_name(settings)
    try:
        exists = await client.collection_exists(name)
        if exists and reuse_existing:
            await verify_index_integrity(client, settings)
            print(f"Verified existing Chitti ontology collection {name}.")
            return
        models = load_models(settings)
        dense_vectors, sparse_vectors = await asyncio.gather(
            asyncio.to_thread(lambda: list(models.dense.passage_embed(texts))),
            asyncio.to_thread(lambda: list(models.sparse.passage_embed(texts))),
        )
        if exists:
            await client.delete_collection(name)
        await client.create_collection(
            name,
            vectors_config={
                DENSE_VECTOR: VectorParams(size=settings.dense_dimensions, distance=Distance.COSINE)
            },
            sparse_vectors_config={
                SPARSE_VECTOR: SparseVectorParams(modifier=Modifier.IDF)
            },
            metadata={
                "source_version": source["version"],
                "ontology_identity": ontology_content_identity(source),
            },
        )
        points = []
        for passage, retrieval_text, dense, sparse in zip(
            passages, texts, dense_vectors, sparse_vectors, strict=True
        ):
            if len(dense) != settings.dense_dimensions:
                raise RuntimeError(
                    f"Dense vector has {len(dense)} dimensions; expected {settings.dense_dimensions}."
                )
            points.append(PointStruct(
                id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"chitti:{source['version']}:{passage['id']}")),
                vector={
                    DENSE_VECTOR: dense.tolist(),
                    SPARSE_VECTOR: SparseVector(
                        indices=sparse.indices.tolist(), values=sparse.values.tolist()
                    ),
                },
                payload={
                    "passage_id": passage["id"],
                    "source": passage["source"],
                    "version": source["version"],
                    "content": passage["content"],
                    "retrieval_text": retrieval_text,
                    "metadata": passage_metadata(passage),
                },
            ))
        await client.upsert(name, points=points, wait=True)
        print(f"Indexed {len(points)} Chitti ontology passages into {name}.")
    finally:
        await client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reuse-existing", action="store_true",
                        help="Verify an existing collection; never replace it.")
    asyncio.run(build(reuse_existing=parser.parse_args().reuse_existing))
