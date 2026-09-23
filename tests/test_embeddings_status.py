"""Settings → Embeddings answered 500: documents.embedder_reachable()
called embedders.local.is_available(), which the torch-free embedder
no longer has. Found by the e2e crawler."""

from tests.conftest import login_client


def test_embeddings_status_answers(fresh_app, monkeypatch):
    from backend import documents
    monkeypatch.setattr(documents, "EMBED_BACKEND", "auto")
    client, _ = login_client(fresh_app, role="platform_admin")
    r = client.get("/api/embeddings/status")
    assert r.status_code == 200, r.text
    assert isinstance(r.json()["embedder"]["reachable"], bool)
