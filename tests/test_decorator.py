"""Python decorator API: @remember, session(), secret redaction."""
import pytest


@pytest.fixture(autouse=True)
def _reset(tmpdb):
    """Reset the decorator's singleton store between tests."""
    import noshy
    from store import NoshyStore
    noshy.reset_store(NoshyStore(db_path=tmpdb, embedder=None))
    yield
    noshy.reset_store(None)


def test_remember_explicit_topic():
    import noshy

    @noshy.remember(topic="greet", importance="high")
    def greet(name):
        return f"hello {name}"

    assert greet("world") == "hello world"
    hits = noshy.recall("greet")
    assert any("hello world" in (h.get("summary") or "") for h in hits)


def test_remember_no_args_form():
    import noshy

    @noshy.remember
    def add(a, b):
        return a + b

    add(2, 3)
    topics = {h.get("topic") for h in noshy.recall("add")}
    assert any(t and "add" in t for t in topics)


def test_remember_captures_errors():
    import noshy

    @noshy.remember(topic="will-fail")
    def boom():
        raise ValueError("nope")

    with pytest.raises(ValueError):
        boom()

    hits = noshy.recall("will-fail-error")
    assert any("ValueError" in (h.get("summary") or "") for h in hits)


def test_remember_skip_if_suppresses_unwanted():
    import noshy

    @noshy.remember(topic="maybe", skip_if=lambda r: r is None)
    def maybe(v):
        return v

    maybe(None)
    maybe("keep-this")
    text = " ".join((h.get("summary") or "") for h in noshy.recall("maybe"))
    assert "keep-this" in text
    assert "None" not in text


def test_remember_redacts_secrets_by_param_name():
    import noshy

    @noshy.remember(topic="login", capture_args=True)
    def login(user, password):
        return {"ok": True}

    login("alice", "hunter2")
    text = " ".join((h.get("summary") or "") for h in noshy.recall("login"))
    assert "hunter2" not in text
    assert "alice" in text


def test_session_scopes_project_and_isolates():
    import noshy

    @noshy.remember(topic="scoped")
    def f():
        return "scoped-result"

    with noshy.session(project="proj-a"):
        f()

    assert any("scoped-result" in (h.get("summary") or "")
               for h in noshy.recall("scoped", project="proj-a"))
    # And not in the default project
    other = [h for h in noshy.recall("scoped", project="default")
             if h.get("topic") == "scoped"]
    assert other == []
