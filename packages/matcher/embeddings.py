"""Sentence-transformer embedding matcher for cross-platform market discovery."""
from __future__ import annotations

import asyncio
import pickle
from pathlib import Path
from typing import Any

import numpy as np
import structlog

log = structlog.get_logger()

# Lazy-loaded to avoid import cost at startup
_model = None
_chroma_client = None
_collection = None

EMBED_MODEL = "BAAI/bge-large-en-v1.5"
SIMILARITY_THRESHOLD = 0.80
CHROMA_PERSIST_PATH = "/tmp/polysentinel_chroma"


def _get_model():
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer
        log.info("loading_embed_model", model=EMBED_MODEL)
        _model = SentenceTransformer(EMBED_MODEL)
    return _model


def _get_collection():
    global _chroma_client, _collection
    if _collection is None:
        import chromadb
        _chroma_client = chromadb.PersistentClient(path=CHROMA_PERSIST_PATH)
        _collection = _chroma_client.get_or_create_collection(
            name="markets",
            metadata={"hnsw:space": "cosine"},
        )
    return _collection


def encode_texts(texts: list[str]) -> np.ndarray:
    model = _get_model()
    return model.encode(texts, normalize_embeddings=True, show_progress_bar=False)


async def index_markets(markets: list[dict], source: str) -> None:
    """Upsert market embeddings into Chroma."""
    if not markets:
        return

    loop = asyncio.get_event_loop()
    texts = [m.get("question") or m.get("title") or "" for m in markets]
    ids = [m.get("market_id") or m.get("id") or m.get("ticker", "") for m in markets]
    metadatas = [
        {
            "source": source,
            "market_id": ids[i],
            "question": texts[i][:500],
        }
        for i in range(len(markets))
    ]

    embeddings = await loop.run_in_executor(None, encode_texts, texts)

    collection = _get_collection()
    # Chroma upsert in chunks of 500
    chunk = 500
    for start in range(0, len(markets), chunk):
        collection.upsert(
            ids=ids[start : start + chunk],
            embeddings=embeddings[start : start + chunk].tolist(),
            metadatas=metadatas[start : start + chunk],
            documents=texts[start : start + chunk],
        )

    log.info("markets_indexed", source=source, count=len(markets))


async def find_matches(
    query_markets: list[dict],
    target_source: str,
    threshold: float = SIMILARITY_THRESHOLD,
    top_k: int = 3,
) -> list[dict]:
    """
    For each market in query_markets, find similar markets from target_source
    in the Chroma index.

    Returns list of {query_id, query_question, match_id, match_question,
                     match_source, score}
    """
    if not query_markets:
        return []

    loop = asyncio.get_event_loop()
    texts = [m.get("question") or m.get("title") or "" for m in query_markets]
    embeddings = await loop.run_in_executor(None, encode_texts, texts)

    collection = _get_collection()
    results = []

    for i, emb in enumerate(embeddings):
        qr = collection.query(
            query_embeddings=[emb.tolist()],
            n_results=top_k,
            where={"source": target_source},
        )
        if not qr["ids"] or not qr["ids"][0]:
            continue

        for j, match_id in enumerate(qr["ids"][0]):
            score = 1.0 - (qr["distances"][0][j] if qr["distances"] else 0)
            if score >= threshold:
                results.append(
                    {
                        "query_id": query_markets[i].get("market_id")
                        or query_markets[i].get("id", ""),
                        "query_question": texts[i],
                        "match_id": match_id,
                        "match_question": qr["documents"][0][j] if qr["documents"] else "",
                        "match_source": target_source,
                        "score": round(score, 4),
                    }
                )

    log.info("embedding_matches_found", count=len(results))
    return results
