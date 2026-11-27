import hashlib
import math
import re
from functools import lru_cache

import chromadb
from chromadb.api.types import Documents, EmbeddingFunction, Embeddings
from chromadb.config import Settings as ChromaSettings
from chromadb.utils.embedding_functions import register_embedding_function

from app.core.config import get_settings
from app.models.schemas import Runbook


@register_embedding_function
class HashEmbedding(EmbeddingFunction[Documents]):
    """Deterministic bag-of-words hashing embedding: no model download (DRY_RUN and tests)."""

    def __init__(self) -> None:
        pass

    def __call__(self, input: Documents) -> Embeddings:
        vectors = []
        for text in input:
            vector = [0.0] * 256
            for word in re.findall(r"[a-z0-9]+", text.lower()):
                vector[int(hashlib.md5(word.encode()).hexdigest(), 16) % 256] += 1.0
            norm = math.sqrt(sum(v * v for v in vector)) or 1.0
            vectors.append([v / norm for v in vector])
        return vectors

    @staticmethod
    def name() -> str:
        return "auto-oncall-hash"

    def get_config(self) -> dict:
        return {}

    @staticmethod
    def build_from_config(config: dict) -> "HashEmbedding":
        return HashEmbedding()


class RunbookStore:
    def __init__(self, path: str, embedding_function: EmbeddingFunction | None = None):
        client = chromadb.PersistentClient(
            path=path, settings=ChromaSettings(anonymized_telemetry=False)
        )
        # Vectors from different embedding functions are incompatible, so separate collections.
        if embedding_function is None:
            self.collection = client.get_or_create_collection("runbooks")
        else:
            self.collection = client.get_or_create_collection(
                f"runbooks-{embedding_function.name()}", embedding_function=embedding_function
            )

    def add(self, runbooks: list[Runbook]) -> None:
        self.collection.upsert(
            ids=[r.id for r in runbooks],
            documents=[f"{r.title}\n{r.content}" for r in runbooks],
            metadatas=[{"title": r.title} for r in runbooks],
        )

    def search(self, query: str, top_k: int = 2) -> list[Runbook]:
        count = self.collection.count()
        if count == 0:
            return []
        result = self.collection.query(query_texts=[query], n_results=min(top_k, count))
        return [
            Runbook(id=i, title=m["title"], content=d)
            for i, d, m in zip(
                result["ids"][0], result["documents"][0], result["metadatas"][0], strict=True
            )
        ]


@lru_cache
def get_runbook_store() -> RunbookStore:
    s = get_settings()
    # DRY_RUN must not download Chroma's default model.
    return RunbookStore(s.chroma_path, HashEmbedding() if s.dry_run else None)
