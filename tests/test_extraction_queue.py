"""Async extraction queue: round-trip, status transitions, failure isolation."""


def test_queue_then_get_pending_round_trip(tmpdb):
    from store import NoshyStore
    s = NoshyStore(db_path=tmpdb, embedder=None)

    pid = s.queue_extraction(
        "Some raw transcript text to extract from later.",
        session_id="sess-1", source="test")
    assert pid

    pending = s.get_pending_extractions()
    assert len(pending) == 1
    row = pending[0]
    assert row["id"] == pid
    assert row["status"] == "pending"
    assert row["session_id"] == "sess-1"
    assert row["source"] == "test"
    assert row["extracted_at"] is None


def test_process_queue_marks_rows_done(tmpdb, monkeypatch):
    """A successful extract_facts call should mark the row 'done' and the
    yielded memories should land in the store via apply_extracted_facts."""
    from store import NoshyStore
    import extractor

    s = NoshyStore(db_path=tmpdb, embedder=None)
    s.queue_extraction("Anything — the LLM is stubbed.")
    s.queue_extraction("Second pending item.")

    # Deterministic stub instead of a live LLM
    fake_facts = [
        {"topic": "stub-fact", "summary": "A fact synthesised by the stub.",
         "importance": "medium", "keywords": ["stub"]},
    ]
    monkeypatch.setattr(extractor, "extract_facts", lambda *_a, **_kw: fake_facts)

    counts = s.process_extraction_queue(limit=10, project="default")
    assert counts["processed"] == 2
    assert counts["stored"] == 2  # one memory per pending row
    assert counts["failed"] == 0
    # Both rows transition to 'done'
    assert s.get_pending_extractions(status="pending") == []
    done = s.get_pending_extractions(status="done")
    assert len(done) == 2
    for r in done:
        assert r["extracted_at"]


def test_process_queue_isolates_failures(tmpdb, monkeypatch):
    """If extract_facts raises on one item, that row should be marked
    'failed' and the rest of the batch must still be processed."""
    from store import NoshyStore
    import extractor

    s = NoshyStore(db_path=tmpdb, embedder=None)
    bad_id = s.queue_extraction("This one will blow up.")
    good_id = s.queue_extraction("This one is fine.")

    def flaky(transcript, **_kw):
        if "blow up" in transcript:
            raise RuntimeError("synthetic extraction failure")
        return [{"topic": "ok", "summary": "Extracted just fine.",
                 "importance": "low"}]

    monkeypatch.setattr(extractor, "extract_facts", flaky)

    counts = s.process_extraction_queue(limit=10)
    assert counts["processed"] == 1
    assert counts["failed"] == 1
    # Failure didn't stall the batch — good row is done, bad row is failed.
    failed = s.get_pending_extractions(status="failed")
    done = s.get_pending_extractions(status="done")
    assert {r["id"] for r in failed} == {bad_id}
    assert {r["id"] for r in done} == {good_id}


def test_queue_mcp_tools_registered():
    from server import MCP_TOOLS
    names = {t["name"] for t in MCP_TOOLS}
    assert "noshy_queue_extraction" in names
    assert "noshy_process_queue" in names
