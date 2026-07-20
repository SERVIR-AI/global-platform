"""E2 — embeddings via a provider's OpenAI-compat /embeddings endpoint.
Swap the backend by handing Corpus any object with .embed(texts) -> (n, d) array."""

from __future__ import annotations

import numpy as np

from ..config import get_settings
from ..llm import build_client


class ProviderEmbedder:
    """Provider set separately from chat (claude serves no embeddings)."""

    _BATCH = 100  # inputs per API call; providers cap batch sizes

    def __init__(self, provider=None, model=None):
        settings = get_settings()
        self.provider = provider or settings.embedding_provider
        self.model = model or settings.embedding_model

    def embed(self, texts: list[str]) -> np.ndarray:
        if not texts:  # never a (0,)-shaped footgun
            return np.zeros((0, 0), dtype=np.float32)
        client = build_client(self.provider)
        vectors: list[list[float]] = []
        for i in range(0, len(texts), self._BATCH):
            resp = client.embeddings.create(model=self.model,
                                            input=texts[i:i + self._BATCH])
            vectors.extend(d.embedding for d in resp.data)
        arr = np.asarray(vectors, dtype=np.float32)
        # unit-normalize so cosine similarity downstream is a plain dot product
        return arr / np.maximum(np.linalg.norm(arr, axis=1, keepdims=True), 1e-12)
