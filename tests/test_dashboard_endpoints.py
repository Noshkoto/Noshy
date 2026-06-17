"""New HTTP endpoints powering the dashboard (search, clusters, DELETE)."""
import json
import socket
import struct
import threading
import time
import urllib.request
import urllib.error
import pytest


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_ready(port, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=0.5).read()
            return
        except (OSError, urllib.error.URLError):
            time.sleep(0.05)
    raise TimeoutError(f"server on :{port} not ready")


class FakeEmbedder:
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
def server(tmpdb):
    """Server with the FakeEmbedder so semantic-search paths work in tests."""
    import server as srv
    from store import NoshyStore
    srv.store = NoshyStore(db_path=tmpdb, embedder=FakeEmbedder())
    # Pre-seed the embedder on run_http's NoshyStore by reaching in
    port = _free_port()
    t = threading.Thread(target=srv.run_http,
                         kwargs={"host": "127.0.0.1", "port": port, "db_path": tmpdb},
                         daemon=True)
    t.start()
    _wait_ready(port)
    # The thread re-created a store via auto_embedder() (none); replace it.
    srv.store = NoshyStore(db_path=tmpdb, embedder=FakeEmbedder())
    yield port, srv.store


def _get(port, path):
    return urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=5)


def _delete(port, path):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method="DELETE")
    return urllib.request.urlopen(req, timeout=5)


def test_dashboard_renders_polish_features(server):
    port, _ = server
    html = _get(port, "/").read().decode()
    # New dashboard markers
    assert 'data-theme="light"' in html
    assert "projectFilter" in html
    assert "Clusters" in html
    assert "loadProjects" in html
    assert "openClusters" in html


def test_memories_search_via_query_param(server):
    port, store = server
    store.store_memory(topic="deploy", summary="Production deploys go through Cloudflare Pages every Friday.")
    store.store_memory(topic="billing", summary="Stripe webhook handler validates the signature header.")
    body = json.loads(_get(port, "/memories?q=cloudflare").read())
    topics = [m["topic"] for m in body["memories"]]
    # The keyword-matching memory must be present and ranked first
    assert "deploy" in topics
    assert topics[0] == "deploy"


def test_clusters_endpoint(server):
    port, store = server
    # Two semantically near-identical memories under different topics
    store.store_memory(topic="a", summary="cache invalidation version key deploy region rollout")
    store.store_memory(topic="b", summary="cache invalidation version key deploy region rollout")
    store.store_memory(topic="c", summary="unrelated billing invoice content nothing in common")
    body = json.loads(_get(port, "/clusters?threshold=0.99").read())
    clusters = body["clusters"]
    flat_topics = [m["topic"] for c in clusters for m in c]
    assert "a" in flat_topics and "b" in flat_topics
    assert "c" not in flat_topics


def test_delete_memory_endpoint(server):
    port, store = server
    mid = store.store_memory(topic="delme", summary="A memory to delete via the DELETE endpoint test.")
    r = _delete(port, f"/memories/{mid}")
    assert r.status == 200
    body = json.loads(r.read())
    assert body["deleted"] == mid
    assert store.get_stats()["memory_count"] == 0


def test_delete_unknown_memory_returns_404(server):
    port, _ = server
    with pytest.raises(urllib.error.HTTPError) as exc:
        _delete(port, "/memories/nope")
    assert exc.value.code == 404


def test_memories_project_filter(server):
    port, store = server
    store.store_memory(topic="x", summary="Memory in project alpha for the project-filter test.", project="alpha")
    store.store_memory(topic="y", summary="Memory in project beta for the project-filter test.", project="beta")
    body = json.loads(_get(port, "/memories?project=alpha").read())
    assert {m["topic"] for m in body["memories"]} == {"x"}
