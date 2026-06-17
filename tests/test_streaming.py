"""Streaming extraction MCP tool and helpers."""


def test_split_transcript_respects_paragraph_boundaries():
    from server import _split_transcript
    text = "para one\n\npara two\n\npara three\n\npara four"
    chunks = _split_transcript(text, target=20)
    # Each chunk is at most ~paragraph-sized, joined by the double newline
    assert len(chunks) >= 2
    # Round-trip should still contain every paragraph
    joined = "\n\n".join(chunks)
    for p in ("para one", "para two", "para three", "para four"):
        assert p in joined


def test_split_transcript_hard_slices_giant_paragraph():
    from server import _split_transcript
    text = "x" * 5000
    chunks = _split_transcript(text, target=1000)
    # Hard-slice path keeps each chunk at most ~2*target chars
    assert all(len(c) <= 2000 for c in chunks)
    assert sum(len(c) for c in chunks) == 5000


def test_short_transcript_returns_one_chunk():
    from server import _split_transcript
    assert _split_transcript("only a little text", target=4000) == ["only a little text"]


def test_stream_extract_handles_empty_input():
    from extractor import stream_extract
    assert list(stream_extract([])) == []
    assert list(stream_extract(["", "   ", "\n\n"])) == []


def test_stream_extract_tool_listed():
    from server import MCP_TOOLS
    names = {t["name"] for t in MCP_TOOLS}
    assert "noshy_stream_extract" in names


def test_stream_extract_tool_no_op_with_no_llm(tmpdb):
    """Without a reachable LLM the tool returns a clean message — it must not crash."""
    import server as srv
    from store import NoshyStore
    srv.store = NoshyStore(db_path=tmpdb, embedder=None)
    r = srv.handle_tools_call({
        "name": "noshy_stream_extract",
        "arguments": {"transcript": "a" * 500, "chunk_chars": 200},
    })
    # No LLM → no facts → no error and zero memories stored
    assert not r.get("isError")
    assert "stored 0 memories" in r["content"][0]["text"]
    assert srv.store.get_stats()["memory_count"] == 0
