"""n8n-backed connectors are listed only on a box that opts in.

Until 2026-10-06 Gmail, Twilio and the n8n echo were registered on every
box, so the assistant named them as integrations the household has.
"""
from backend import connectors


def _spec(name: str, backend: str) -> connectors.ConnectorSpec:
    return connectors.ConnectorSpec(
        name=name, description="test", params_schema={"type": "object", "properties": {}},
        invoke=None, backend=backend)


def test_n8n_connector_is_not_registered_by_default(monkeypatch):
    monkeypatch.delenv("YORIK_N8N_CONNECTORS", raising=False)
    connectors.register(_spec("test-n8n-off", "n8n"))
    assert connectors.get("test-n8n-off") is None
    assert not [s for s in connectors.list_all() if s.backend == "n8n"]


def test_n8n_connector_registers_when_switched_on(monkeypatch):
    monkeypatch.setenv("YORIK_N8N_CONNECTORS", "1")
    try:
        connectors.register(_spec("test-n8n-on", "n8n"))
        assert connectors.get("test-n8n-on") is not None
    finally:
        connectors._REGISTRY.pop("test-n8n-on", None)


def test_builtin_connectors_are_untouched(monkeypatch):
    monkeypatch.delenv("YORIK_N8N_CONNECTORS", raising=False)
    names = {s.name for s in connectors.list_all()}
    assert {"weather", "maps", "paperless", "immich"} <= names
