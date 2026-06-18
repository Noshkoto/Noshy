"""Contradiction detection: check_contradiction graceful degrade, find +
flag idempotency, recall inline warnings."""
import struct


class FakeEmbedder:
    """Deterministic 8-dim hash embedder so semantic paths exercise."""
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


def test_check_contradiction_degrades_when_no_llm_configured(monkeypatch):
    """With no reachable LLM endpoint, must return the 'unavailable' default
    rather than raising — callers should never have to special-case this."""
    monkeypatch.setenv("NOSHY_API_BASE", "http://127.0.0.1:1/v1")  # unreachable
    monkeypatch.delenv("NOSHY_API_KEY", raising=False)
    monkeypatch.delenv("API_SERVER_KEY", raising=False)
    from extractor import check_contradiction
    r = check_contradiction(
        "user prefers Python for backend work",
        "user moved to Rust after kernel performance issues",
    )
    assert r["contradicts"] is False
    assert r["confidence"] == 0.0
    assert r["explanation"] == "unavailable"


def test_find_contradictions_empty_when_fewer_than_two_embedded_memories(tmpdb):
    from store import NoshyStore
    s = NoshyStore(db_path=tmpdb, embedder=FakeEmbedder())
    # Zero memories
    assert s.find_contradictions() == []
    # One memory — still < 2
    s.store_memory(topic="solo", summary="A single memory with a real embedding.")
    assert s.find_contradictions() == []


def test_flag_contradictions_is_idempotent(tmpdb, monkeypatch):
    """flag_contradictions must skip pairs that already have a 'contradicts'
    edge between them, so repeated sweep runs don't pile up duplicates."""
    from store import NoshyStore
    import extractor

    s = NoshyStore(db_path=tmpdb, embedder=FakeEmbedder())
    # Two memories that share enough words to land in the similarity band
    # (FakeEmbedder is hash-based, so overlapping vocab → moderate cosine).
    s.store_memory(topic="lang-pref",
                   summary="user prefers Python for backend services today")
    s.store_memory(topic="lang-pref-later",
                   summary="user prefers Rust for backend services today")

    # Stub the LLM so we don't depend on a live endpoint.
    def fake_check(a, b, **kw):
        return {"contradicts": True, "confidence": 0.9,
                "explanation": "opposite language choice for same role"}
    monkeypatch.setattr(extractor, "check_contradiction", fake_check)

    n1 = s.flag_contradictions(max_llm_checks=10)
    n2 = s.flag_contradictions(max_llm_checks=10)
    # Run 1 should create an edge if the pair lands in the band.
    # Run 2 must NOT add another edge — that's the idempotency guarantee.
    assert n2 == 0
    edge_count = s.conn.execute(
        "SELECT COUNT(*) FROM memory_edges WHERE relation = 'contradicts'"
    ).fetchone()[0]
    # Whatever was created in run 1, run 2 didn't double it.
    assert edge_count == n1


def test_recall_inlines_contradicts_warning(tmpdb):
    """When a recalled memory has a contradicts edge, the formatted recall
    output should prepend a warning line."""
    import server as srv
    from store import NoshyStore

    s = NoshyStore(db_path=tmpdb, embedder=FakeEmbedder())
    a_id = s.store_memory(
        topic="lang-pref",
        summary="user prefers Python for backend services today")
    b_id = s.store_memory(
        topic="lang-pref-later",
        summary="user prefers Rust for backend services today")
    s.link_memories(a_id, b_id, relation="contradicts", strength=0.9)
    srv.store = s

    r = srv.handle_tools_call({
        "name": "noshy_recall",
        "arguments": {"query": "lang-pref", "mode": "keyword"},
    })
    text = r["content"][0]["text"]
    assert "conflicts with" in text


def test_find_contradictions_mcp_tool_registered():
    from server import MCP_TOOLS
    names = {t["name"] for t in MCP_TOOLS}
    assert "noshy_find_contradictions" in names
