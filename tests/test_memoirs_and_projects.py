"""Memoir recall, project isolation."""


def test_memoir_keyword_recall(store):
    store.store_memoir(title="Runbook", content="Deploys go through Cloudflare Pages with GitHub Actions.")
    hits = store.recall_memoirs("cloudflare")
    assert len(hits) == 1 and hits[0]["title"] == "Runbook"


def test_memoir_semantic_recall_via_embedder(fake_store):
    fake_store.store_memoir(title="Pods", content="kubectl rollout restart deployment in production namespace")
    fake_store.store_memoir(title="Billing", content="Invoices are generated monthly via Stripe and emailed")
    # Query shares words with the first memoir but not the title token
    hits = fake_store.recall_memoirs("kubectl rollout")
    titles = [h["title"] for h in hits]
    assert "Pods" in titles


def test_memoirs_show_up_in_hybrid_recall(fake_store):
    fake_store.store_memoir(title="Doc", content="invoices stripe billing finance accounting")
    fake_store.store_memory(topic="task", summary="Look at invoices stripe billing report numbers")
    hits = fake_store.recall_hybrid("invoices")
    kinds = [h.get("_kind") for h in hits]
    assert "memoir" in kinds


def test_list_and_delete_project(store):
    store.store_memory(topic="x", summary="A seed memory inside the alpha project for listing test.", project="alpha")
    store.store_memory(topic="y", summary="A seed memory inside the beta project for listing test.", project="beta")
    store.store_memoir(title="d", content="Some memoir content under the alpha project.", project="alpha")
    projs = {p["project"]: p for p in store.list_projects()}
    assert "alpha" in projs and "beta" in projs
    assert projs["alpha"]["memoir_count"] == 1
    counts = store.delete_project("alpha")
    assert counts == {"memories": 1, "memoirs": 1}
    projs2 = {p["project"] for p in store.list_projects()}
    assert "alpha" not in projs2 and "beta" in projs2


def test_delete_project_guards(store):
    import pytest
    with pytest.raises(ValueError):
        store.delete_project("")
    with pytest.raises(ValueError):
        store.delete_project("*")
