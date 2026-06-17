"""
Noshy public Python API.

    import noshy

    @noshy.remember(topic="deploy", importance="high")
    def deploy(env): ...

    with noshy.session(project="my-project"):
        deploy("prod")

    noshy.recall("deploy")
"""
from decorator import remember, session, recall, get_store, reset_store
from store import NoshyStore
from embed import auto_embedder

__all__ = [
    "remember",
    "session",
    "recall",
    "get_store",
    "reset_store",
    "NoshyStore",
    "auto_embedder",
]
