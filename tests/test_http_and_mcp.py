"""HTTP server, dashboard, MCP handlers — including auth."""
import json
import threading
import time
import urllib.request
import urllib.error
import socket
import pytest


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_ready(port, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                # /health is always public; use it as the readiness check
                urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=0.5).read()
                return
        except (OSError, urllib.error.URLError):
            time.sleep(0.05)
    raise TimeoutError(f"server on :{port} not ready")


@pytest.fixture
def server(tmpdb):
    """Start the HTTP server in a thread; return (port, store)."""
    import server as srv
    from store import NoshyStore
    srv.store = NoshyStore(db_path=tmpdb, embedder=None)
    port = _free_port()
    t = threading.Thread(target=srv.run_http,
                         kwargs={"host": "127.0.0.1", "port": port, "db_path": tmpdb},
                         daemon=True)
    t.start()
    _wait_ready(port)
    yield port, srv.store


@pytest.fixture
def server_with_auth(tmpdb, monkeypatch):
    token = "test-secret-token-xyz"
    monkeypatch.setenv("NOSHY_HTTP_TOKEN", token)
    import server as srv
    from store import NoshyStore
    srv.store = NoshyStore(db_path=tmpdb, embedder=None)
    port = _free_port()
    t = threading.Thread(target=srv.run_http,
                         kwargs={"host": "127.0.0.1", "port": port, "db_path": tmpdb},
                         daemon=True)
    t.start()
    _wait_ready(port)
    yield port, token


def _get(port, path, token=None):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    return urllib.request.urlopen(req, timeout=5)


def _post(port, path, payload, token=None):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    return urllib.request.urlopen(req, timeout=5)


# ──────────── Dashboard / GET endpoints ────────────

def test_dashboard_html_served(server):
    port, _ = server
    r = _get(port, "/")
    assert r.status == 200
    body = r.read().decode()
    assert "<title>Noshy" in body
    assert "loadStats" in body


def test_memories_endpoint(server):
    port, store = server
    store.store_memory(topic="alpha", summary="First memory listed via the memories endpoint test.")
    store.store_memory(topic="beta", summary="Second memory listed via the memories endpoint test.")
    body = json.loads(_get(port, "/memories?limit=10").read())
    assert {m["topic"] for m in body["memories"]} == {"alpha", "beta"}


def test_projects_endpoint(server):
    port, store = server
    store.store_memory(topic="x", summary="A seed memory for the projects endpoint test.", project="alpha")
    body = json.loads(_get(port, "/projects").read())
    assert any(p["project"] == "alpha" for p in body["projects"])


def test_health_endpoint(server):
    port, _ = server
    assert json.loads(_get(port, "/health").read())["status"] == "ok"


def test_unknown_route_returns_404(server):
    port, _ = server
    with pytest.raises(urllib.error.HTTPError) as exc:
        _get(port, "/nope")
    assert exc.value.code == 404


# ──────────── tools/call dispatch ────────────

def test_tools_call_store_and_recall_via_http(server):
    port, _ = server
    r = json.loads(_post(port, "/tools/call", {
        "name": "noshy_store_memory",
        "arguments": {"topic": "deploy", "summary": "Production deploys go through Cloudflare Pages with GitHub Actions."},
    }).read())
    assert not r.get("isError")

    r = json.loads(_post(port, "/tools/call", {
        "name": "noshy_recall",
        "arguments": {"query": "cloudflare", "mode": "hybrid"},
    }).read())
    assert not r.get("isError")
    assert "Production" in r["content"][0]["text"]


def test_concurrent_writes_thread_safe(server):
    port, store = server
    errors = []

    def worker(i):
        try:
            _post(port, "/tools/call", {
                "name": "noshy_store_memory",
                "arguments": {"topic": f"conc-{i}",
                              "summary": f"Concurrent write number {i} to verify thread-local connections."},
            })
        except Exception as e:
            errors.append(str(e))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(15)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert store.get_stats()["memory_count"] == 15


# ──────────── HTTP auth ────────────

def test_unauthorized_request_rejected(server_with_auth):
    port, _ = server_with_auth
    with pytest.raises(urllib.error.HTTPError) as exc:
        _get(port, "/stats")
    assert exc.value.code == 401


def test_wrong_token_rejected(server_with_auth):
    port, _ = server_with_auth
    with pytest.raises(urllib.error.HTTPError) as exc:
        _get(port, "/stats", token="wrong")
    assert exc.value.code == 401


def test_correct_token_accepted(server_with_auth):
    port, token = server_with_auth
    body = json.loads(_get(port, "/stats", token=token).read())
    assert "memory_count" in body


def test_health_and_dashboard_remain_public_with_auth(server_with_auth):
    port, _ = server_with_auth
    # /health and / are public so tools and humans can probe without a token
    assert _get(port, "/health").status == 200
    assert _get(port, "/").status == 200
