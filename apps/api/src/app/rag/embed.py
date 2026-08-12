"""E2 — embeddings. Two backends, one contract.

Swap the backend by handing Corpus any object with .embed(texts) -> (n, d) array
and `.provider` / `.model` attributes (the corpus records those and refuses to mix).
"""

from __future__ import annotations

import numpy as np

from ..config import get_settings
from ..llm import MissingAPIKey, build_client


class ProviderEmbedder:
    """A provider's OpenAI-compat /embeddings endpoint. Provider is set separately
    from chat (claude serves no embeddings)."""

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
        return _unit(vectors)


class VertexEmbedder:
    """Vertex AI embeddings, authenticated by the ambient service-account identity.

    Deliberately NOT on the OpenAI-compat client: Vertex serves embedding models
    only on its native predict endpoint, and authenticates with short-lived tokens
    rather than a static key. That token is the point — deployed, there is no API
    key on disk to leak or rotate.
    """

    _BATCH = 20   # conservative: the real cap is per-request tokens, not instances

    def __init__(self, model=None, project=None, location=None, dimensions=None):
        s = get_settings()
        self.provider = "vertex"
        self.model = model or s.embedding_model
        self.location = location or s.vertex_location
        self.dimensions = dimensions if dimensions is not None else s.embedding_dimensions
        self._project = project or s.vertex_project
        self._session = None

    def _authorized(self):
        """Credentials come from ADC: the runtime service account when deployed, a
        developer login locally. AuthorizedSession refreshes the token itself."""
        if self._session is not None:
            return self._session, self._project
        try:
            import google.auth
            from google.auth.transport.requests import AuthorizedSession
        except ImportError as exc:                       # declared, never guessed at
            raise MissingAPIKey(
                "Vertex embeddings need the 'google-auth' package") from exc
        try:
            creds, adc_project = google.auth.default(
                scopes=["https://www.googleapis.com/auth/cloud-platform"])
        except Exception as exc:
            raise MissingAPIKey(
                f"no Google credentials for Vertex embeddings ({exc}) — deploy with a "
                "service account, or run `gcloud auth application-default login`") from exc
        project = self._project or adc_project
        if not project:
            raise MissingAPIKey("no GCP project for Vertex embeddings — set VERTEX_PROJECT")
        self._session, self._project = AuthorizedSession(creds), project
        return self._session, project

    def embed(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, 0), dtype=np.float32)
        session, project = self._authorized()
        url = (f"https://{self.location}-aiplatform.googleapis.com/v1/projects/{project}"
               f"/locations/{self.location}/publishers/google/models/{self.model}:predict")
        vectors: list[list[float]] = []
        for i in range(0, len(texts), self._BATCH):
            body: dict = {"instances": [{"content": t} for t in texts[i:i + self._BATCH]]}
            if self.dimensions:
                body["parameters"] = {"outputDimensionality": self.dimensions}
            resp = session.post(url, json=body, timeout=120)
            if resp.status_code != 200:                  # surface the reason, don't swallow it
                raise MissingAPIKey(
                    f"Vertex embeddings refused ({resp.status_code}): {resp.text[:200]}")
            vectors.extend(p["embeddings"]["values"] for p in resp.json()["predictions"])
        return _unit(vectors)


def _unit(vectors: list[list[float]]) -> np.ndarray:
    """Unit-normalize so cosine similarity downstream is a plain dot product."""
    arr = np.asarray(vectors, dtype=np.float32)
    return arr / np.maximum(np.linalg.norm(arr, axis=1, keepdims=True), 1e-12)


def get_embedder():
    """The configured embedder. Corpus records provider+model and refuses to serve a
    corpus embedded by a different one, so this is not hot-swappable."""
    return VertexEmbedder() if get_settings().embedding_provider == "vertex" else ProviderEmbedder()
