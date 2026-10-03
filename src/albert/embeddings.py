from __future__ import annotations

import hashlib
import math
import re
from functools import lru_cache
from typing import Protocol

import httpx

from albert.config import get_settings


class Embedder(Protocol):
    name: str
    dimensions: int

    def embed(self, text: str) -> list[float]: ...


class HashingEmbedder:
    """Dependency-free development fallback; not a semantic model."""

    def __init__(self, dimensions: int) -> None:
        self.dimensions = dimensions
        self.name = f"hashing-v1-{dimensions}"

    def embed(self, text: str) -> list[float]:
        vector = [0.0] * self.dimensions
        tokens = re.findall(r"[\w.-]+", text.lower())
        for token in tokens:
            digest = hashlib.blake2b(token.encode(), digest_size=16).digest()
            index = int.from_bytes(digest[:8], "big") % self.dimensions
            sign = 1.0 if digest[8] & 1 else -1.0
            vector[index] += sign
        norm = math.sqrt(sum(value * value for value in vector))
        return [value / norm for value in vector] if norm else vector


class SentenceTransformerEmbedder:
    def __init__(self, model: str, dimensions: int) -> None:
        from sentence_transformers import SentenceTransformer

        self._model = SentenceTransformer(model)
        actual = self._model.get_sentence_embedding_dimension()
        if actual != dimensions:
            raise ValueError(f"Embedding model dimensions {actual} do not match {dimensions}")
        self.dimensions = dimensions
        self.name = model

    def embed(self, text: str) -> list[float]:
        vector = self._model.encode(text, normalize_embeddings=True)
        return [float(value) for value in vector]


class OpenAICompatibleEmbedder:
    def __init__(self, model: str, dimensions: int) -> None:
        settings = get_settings()
        if settings.openai_api_key is None:
            raise ValueError("ALBERT_OPENAI_API_KEY is required")
        self.base_url = settings.openai_base_url.rstrip("/")
        self.api_key = settings.openai_api_key.get_secret_value()
        self.name = model
        self.dimensions = dimensions

    def embed(self, text: str) -> list[float]:
        payload: dict[str, object] = {"model": self.name, "input": text}
        if get_settings().embedding_request_dimensions:
            payload["dimensions"] = self.dimensions
        response = httpx.post(
            f"{self.base_url}/embeddings",
            headers={"Authorization": f"Bearer {self.api_key}"},
            json=payload,
            timeout=60,
        )
        response.raise_for_status()
        vector = response.json()["data"][0]["embedding"]
        if len(vector) != self.dimensions:
            raise ValueError("Embedding provider returned unexpected dimensions")
        norm = math.sqrt(sum(float(value) ** 2 for value in vector))
        return [float(value) / norm for value in vector] if norm else [float(v) for v in vector]


@lru_cache
def get_embedder() -> Embedder:
    settings = get_settings()
    if settings.embedding_provider == "sentence-transformers":
        return SentenceTransformerEmbedder(
            settings.embedding_model, settings.embedding_dimensions
        )
    if settings.embedding_provider == "openai-compatible":
        return OpenAICompatibleEmbedder(settings.embedding_model, settings.embedding_dimensions)
    return HashingEmbedder(settings.embedding_dimensions)
