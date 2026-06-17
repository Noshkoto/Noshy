"""Core store: CRUD, dedup, TTL, decay, consolidation, clustering."""
from datetime import datetime, timedelta, timezone


def test_store_and_recall(store):
    mid = store.store_memory(topic="t", summary="A normal sentence we'll search for shortly.")
    assert mid
    hits = store.recall_by_topic("normal")
    assert any(h["id"] == mid for h in hits)


def test_dedup_returns_same_id(store):
    a = store.store_memory(topic="dup", summary="A clear and stable summary that should dedupe on a second store.")
    b = store.store_memory(topic="dup", summary="A clear and stable summary that should dedupe on a second store.")
    assert a == b
    stats = store.get_stats()
    assert stats["memory_count"] == 1


def test_ttl_expiry_filters_recall_and_purge(store):
    store.store_memory(topic="keep", summary="A memory that should still be visible in recall.")
    store.store_memory(topic="gone", summary="A memory that has already expired.", ttl_seconds=-1)
    assert all(h["topic"] != "gone" for h in store.recall_by_topic("memory"))
    assert store.purge_expired() == 1
    assert store.get_stats()["memory_count"] == 1


def test_decay_protects_critical(store):
    crit = store.store_memory(topic="incident", summary="A production incident memory that must persist for many days.", importance="critical")
    low = store.store_memory(topic="trivia", summary="A low importance memory that can fade out quickly when decay runs.", importance="low")
    # Force several decay rounds
    for _ in range(5):
        store.decay_weights(decay_rate=0.6)
    rows = {r["id"]: r["weight"] for r in store.conn.execute(
        "SELECT id, weight FROM memories").fetchall()}
    # Critical kept above the deletion floor; low may even have been deleted
    assert rows.get(crit, 0) > 0.1


def test_maybe_decay_respects_interval(store):
    store.store_memory(topic="t", summary="A baseline memory to age over time in this test.")
    # First call seeds the clock without decaying
    assert store.maybe_decay() is False
    # Within the interval -> no run
    assert store.maybe_decay() is False
    # Force the clock backwards
    past = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
    store._set_meta("last_decay", past)
    store.conn.commit()
    assert store.maybe_decay() is True
    # And not again immediately
    assert store.maybe_decay() is False


def test_consolidate_topic_prunes_originals(store):
    store.store_memory(topic="bug", summary="Cache returns stale data after deploy.")
    store.store_memory(topic="bug", summary="Fixed cache by adding a version key to bust on deploy.")
    store.store_memory(topic="bug", summary="Verified the cache invalidation works across regions now.")
    assert store.get_stats()["memory_count"] == 3
    removed = store.consolidate("bug")
    assert removed == 2
    assert store.get_stats()["memory_count"] == 1
    survivor = store.conn.execute("SELECT summary, consolidation_count FROM memories WHERE topic='bug'").fetchone()
    assert "stale" in survivor["summary"] and "invalidation" in survivor["summary"]
    assert survivor["consolidation_count"] == 2


def test_cluster_detection_finds_similar(fake_store):
    s = fake_store
    s.store_memory(topic="a", summary="cache invalidation issue version key deploy region")
    s.store_memory(topic="b", summary="cache invalidation issue version key deploy region")
    s.store_memory(topic="c", summary="completely different unrelated billing invoice content")
    clusters = s.find_clusters(threshold=0.99)
    flat_ids = [m["topic"] for cluster in clusters for m in cluster]
    assert "a" in flat_ids and "b" in flat_ids
    assert "c" not in flat_ids
    # Now actually consolidate and confirm row count drops
    before = s.get_stats()["memory_count"]
    counts = s.consolidate_clusters(threshold=0.99)
    assert counts["clusters"] >= 1
    assert s.get_stats()["memory_count"] == before - counts["merged"]


def test_delete_by_id_and_topic(store):
    a = store.store_memory(topic="del", summary="First memory to delete in the delete-by-id test.")
    store.store_memory(topic="del", summary="Second memory to delete by topic later in this test.")
    assert store.delete_memory(a) is True
    assert store.delete_memory(a) is False
    assert store.delete_by_topic("del") == 1
    assert store.get_stats()["memory_count"] == 0


def test_feedback_adjusts_weight(store):
    mid = store.store_memory(topic="rated", summary="A memory we will rate up and down in this test.")
    w0 = store.conn.execute("SELECT weight FROM memories WHERE id=?", (mid,)).fetchone()["weight"]
    store.record_feedback(mid, 1, "useful")
    w1 = store.conn.execute("SELECT weight FROM memories WHERE id=?", (mid,)).fetchone()["weight"]
    assert w1 > w0
    store.record_feedback(mid, -1, "actually wrong")
    w2 = store.conn.execute("SELECT weight FROM memories WHERE id=?", (mid,)).fetchone()["weight"]
    assert w2 < w1
