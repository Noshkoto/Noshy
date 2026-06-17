"""Shared pytest fixtures: temp DBs, no-LLM/no-embed environment, etc."""
import os
import sys
import struct
import tempfile
import pytest

# Make project modules importable when running `pytest` from repo root
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir)))


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch):
    """Force keyword-only mode by default so tests never hit a live LLM."""
    monkeypatch.setenv("NOSHY_EMBED_PROVIDER", "none")
    # Point any module that creates its own NoshyStore at the per-test DB
    yield


@pytest.fixture
def tmpdb(monkeypatch):
    """Per-test database path, also exposed via NOSHY_DB."""
    path = tempfile.mktemp(suffix=".db")
    monkeypatch.setenv("NOSHY_DB", path)
    yield path
    # Best-effort cleanup; SQLite may still hold the WAL file briefly
    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(path + suffix)
        except OSError:
            pass


@pytest.fixture
def store(tmpdb):
    """A fresh NoshyStore with no embedder."""
    from store import NoshyStore
    s = NoshyStore(db_path=tmpdb, embedder=None)
    yield s


class FakeEmbedder:
    """Deterministic toy embedder: hash words into an 8-dim vector. Useful for
    exercising semantic code paths in tests without external services."""
    def dims(self):
        return 8

    def embed(self, texts):
        out = []
        for t in texts:
            v = [0.0] * 8
            for w in (t or "").lower().split():
                v[hash(w) % 8] += 1.0
            out.append(struct.pack("8f", *v))
        return out


@pytest.fixture
def fake_store(tmpdb):
    """NoshyStore wired to FakeEmbedder so vector paths exercise correctly."""
    from store import NoshyStore
    s = NoshyStore(db_path=tmpdb, embedder=FakeEmbedder())
    yield s
