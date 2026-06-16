"""
Noshy — persistent memory for AI agents.

ICM-compatible, MCP-native, works with any LLM.
pip install noshy
"""
import os
import json
import struct
import hashlib
import logging
import urllib.request
import urllib.error
from typing import Optional, List, Dict, Any

log = logging.getLogger("aion.embed")

# ──────────── Embedding Provider Interface ────────────

class Embedder:
    """Base class for embedding providers."""
    def dims(self) -> int: ...
    def embed(self, texts: List[str]) -> List[bytes]: ...


class OpenAIEmbedder(Embedder):
    """OpenAI-compatible embeddings (works with OpenAI, Hermes API, vLLM, etc.)"""
    def __init__(self, api_base: str = None, api_key: str = None, model: str = "text-embedding-3-small"):
        self.api_base = api_base or os.environ.get(
            "NOSHY_EMBED_API_BASE",
            os.environ.get("OPENAI_API_BASE", "https://api.openai.com/v1")
        )
        self.api_key = api_key or os.environ.get("NOSHY_EMBED_API_KEY", os.environ.get("OPENAI_API_KEY", ""))
        self.model = model or os.environ.get("NOSHY_EMBED_MODEL", "text-embedding-3-small")
        self._dims = None

    def dims(self) -> int:
        if self._dims is None:
            self._dims = 1536  # default for text-embedding-3-small
        return self._dims

    def embed(self, texts: List[str]) -> List[bytes]:
        if not texts:
            return []
        body = json.dumps({"model": self.model, "input": texts}).encode()
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        req = urllib.request.Request(
            f"{self.api_base}/embeddings",
            data=body,
            headers=headers,
        )
        try:
            resp = urllib.request.urlopen(req, timeout=30)
            data = json.loads(resp.read())
            return [_pack_floats(d["embedding"]) for d in data["data"]]
        except urllib.error.HTTPError as e:
            log.error(f"OpenAI embed failed: HTTP {e.code} {e.reason}")
            return []
        except Exception as e:
            log.error(f"OpenAI embed failed: {e}")
            return []


class FastembedEmbedder(Embedder):
    """Local embeddings via fastembed — no API key needed."""
    def __init__(self, model: str = "intfloat/multilingual-e5-base"):
        self.model_name = model or os.environ.get("NOSHY_EMBED_MODEL", "intfloat/multilingual-e5-base")
        self._model = None
        self._dims = None

    def _load(self):
        if self._model is None:
            from fastembed import TextEmbedding
            self._model = TextEmbedding(model_name=self.model_name)
            self._dims = self._model._model_description["dim"]
            log.info(f"Loaded fastembed: {self.model_name} ({self._dims}d)")

    def dims(self) -> int:
        self._load()
        return self._dims

    def embed(self, texts: List[str]) -> List[bytes]:
        self._load()
        embeddings = list(self._model.embed(texts))
        return [_pack_floats(emb) for emb in embeddings]


class HermesEmbedder(Embedder):
    """Use the local Hermes API server for embeddings (zero config if running)."""
    def __init__(self):
        self.api_base = "http://127.0.0.1:8642/v1"
        self.api_key = os.environ.get("API_SERVER_KEY", "")
        self._dims = 1536  # default

    def dims(self) -> int:
        return self._dims

    def embed(self, texts: List[str]) -> List[bytes]:
        # Hermes API server doesn't have /embeddings by default
        # Fall through to whatever works
        return []


class NoOpEmbedder(Embedder):
    """No-op embedder — disables semantic search, keyword search still works."""
    def dims(self) -> int:
        return 0

    def embed(self, texts: List[str]) -> List[bytes]:
        return []


# ──────────── Auto-detection ────────────

def auto_embedder() -> Embedder:
    """Detect the best available embedding provider."""
    # 1. Check for explicit config
    provider = os.environ.get("NOSHY_EMBED_PROVIDER", "").lower()

    if provider == "openai":
        return OpenAIEmbedder()
    if provider == "fastembed":
        return FastembedEmbedder()
    if provider == "hermes":
        return HermesEmbedder()
    if provider == "none":
        return NoOpEmbedder()

    # 2. Check for OPENAI_API_KEY
    if os.environ.get("OPENAI_API_KEY"):
        return OpenAIEmbedder()

    # 3. Check for Hermes API server
    try:
        req = urllib.request.Request("http://127.0.0.1:8642/v1/models",
            headers={"Authorization": f"Bearer {os.environ.get('API_SERVER_KEY', '')}"})
        urllib.request.urlopen(req, timeout=3)
        return OpenAIEmbedder(api_base="http://127.0.0.1:8642/v1",
                            api_key=os.environ.get("API_SERVER_KEY", ""))
    except Exception:
        pass

    # 4. Try fastembed (local, no API key)
    try:
        import fastembed
        return FastembedEmbedder()
    except ImportError:
        pass

    # 5. Use Hermes as a last resort
    return HermesEmbedder()


# ──────────── Helpers ────────────

def _pack_floats(arr: List[float]) -> bytes:
    return struct.pack(f'{len(arr)}f', *arr)

def _unpack_floats(data: bytes) -> List[float]:
    count = len(data) // 4
    return list(struct.unpack(f'{count}f', data[:count * 4]))
