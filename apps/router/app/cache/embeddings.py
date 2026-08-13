from __future__ import annotations

import hashlib
import math
import re
from abc import ABC, abstractmethod

_WORD_RE = re.compile(r"[a-z0-9]+", re.IGNORECASE)


class EmbeddingProvider(ABC):
    """Prompt → dense vector for semantic cache similarity."""

    dim: int

    @abstractmethod
    def embed(self, text: str) -> list[float]:
        raise NotImplementedError


class HashingEmbedder(EmbeddingProvider):
    """Lightweight local embedder (no ML deps).

    Feature-hashes word unigrams, word bigrams, and character trigrams into a
    fixed-dim L2-normalized vector. Pair with ``combined_similarity`` (cosine +
    sequence ratio) so near-duplicate / lightly paraphrased prompts hit without
    downloading a model. For deeper paraphrase matching, set
    ``CACHE_EMBEDDING_PROVIDER=sentence-transformers`` (optional extra; no cloud
    embedding API is required). Pair with ``CACHE_INDEX_BACKEND=auto`` so Redis 8
    Query Engine can KNN-index the same vectors.
    """

    def __init__(self, dim: int = 256) -> None:
        if dim < 32:
            raise ValueError("embedding dim must be >= 32")
        self.dim = dim

    def embed(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        words = _WORD_RE.findall(text.lower())
        if not words:
            vec[0] = 1.0
            return vec

        for word in words:
            self._add(vec, word, weight=4.0)
        for i in range(len(words) - 1):
            self._add(vec, f"{words[i]}_{words[i + 1]}", weight=2.0)

        compact = "".join(words)
        for i in range(max(0, len(compact) - 2)):
            self._add(vec, compact[i : i + 3], weight=1.0)

        return _l2_normalize(vec)

    def _add(self, vec: list[float], feature: str, *, weight: float) -> None:
        digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
        idx = int.from_bytes(digest[:4], "little") % self.dim
        sign = 1.0 if digest[4] % 2 == 0 else -1.0
        vec[idx] += sign * weight


def _l2_normalize(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(v * v for v in vec))
    if norm == 0.0:
        out = [0.0] * len(vec)
        out[0] = 1.0
        return out
    return [v / norm for v in vec]


def resize_embedding(vec: list[float], dim: int) -> list[float]:
    """Truncate or pad a vector to ``dim`` and L2-normalize (OpenAI ``dimensions``)."""
    if dim < 32:
        raise ValueError("embedding dim must be >= 32")
    if len(vec) == dim:
        return vec
    if len(vec) > dim:
        return _l2_normalize(vec[:dim])
    return _l2_normalize(vec + [0.0] * (dim - len(vec)))


def cosine_similarity(a: list[float], b: list[float]) -> float:
    if len(a) != len(b):
        raise ValueError("embedding dimensions must match")
    return sum(x * y for x, y in zip(a, b, strict=True))


def sequence_similarity(a: str, b: str) -> float:
    """Cheap string near-duplicate score (difflib ratio on lowercased text)."""
    from difflib import SequenceMatcher

    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


def combined_similarity(
    *,
    query_embedding: list[float],
    stored_embedding: list[float],
    query_prompt: str,
    stored_prompt: str,
) -> float:
    """Max of embedding cosine and string ratio — robust for local hashing demos."""
    cos = cosine_similarity(query_embedding, stored_embedding)
    seq = sequence_similarity(query_prompt, stored_prompt)
    return max(cos, seq)


def get_embedder(*, provider: str = "hashing", dim: int = 256) -> EmbeddingProvider:
    name = provider.lower().strip()
    if name in {"hashing", "hash", "local"}:
        return HashingEmbedder(dim=dim)
    if name in {"sentence-transformers", "st", "sbert"}:
        return _SentenceTransformersEmbedder(dim=dim)
    raise ValueError(f"Unsupported CACHE_EMBEDDING_PROVIDER: {provider}")


class _SentenceTransformersEmbedder(EmbeddingProvider):
    """Optional heavy embedder — requires `pip install '.[embeddings]'`."""

    def __init__(self, dim: int = 384) -> None:
        try:
            from sentence_transformers import SentenceTransformer  # type: ignore[import-untyped]
        except ImportError as exc:  # pragma: no cover - optional path
            raise RuntimeError(
                "CACHE_EMBEDDING_PROVIDER=sentence-transformers requires "
                "`pip install '.[embeddings]'` (sentence-transformers)"
            ) from exc
        self._model = SentenceTransformer("sentence-transformers/all-MiniLM-L6-v2")
        native = int(self._model.get_sentence_embedding_dimension())
        self.dim = dim if dim > 0 else native
        self._native = native

    def embed(self, text: str) -> list[float]:  # pragma: no cover - optional path
        raw = self._model.encode(text or " ", normalize_embeddings=True)
        vec = [float(x) for x in raw.tolist()]
        if len(vec) == self.dim:
            return vec
        if len(vec) > self.dim:
            return _l2_normalize(vec[: self.dim])
        return _l2_normalize(vec + [0.0] * (self.dim - len(vec)))
