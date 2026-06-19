"""Echo connector — n8n-backed sanity check.

This isn't useful by itself; it exists so we can verify the whole
Yorik↔n8n pipeline (import workflow → activate → POST webhook → response)
without depending on any external service or OAuth setup.

After install_connector, invoking it returns whatever payload you pass back.
"""

from __future__ import annotations

import json
from pathlib import Path

from . import ConnectorSpec, register

_TEMPLATE_PATH = Path(__file__).resolve().parent.parent.parent / "connector_templates" / "echo.workflow.json"

with open(_TEMPLATE_PATH) as f:
    _WORKFLOW_TEMPLATE = json.load(f)


register(ConnectorSpec(
    name="n8n-echo",
    description=(
        "End-to-end test connector that proves the n8n integration works. "
        "Whatever JSON you send back, it echoes — useful for verifying that "
        "install_connector imported the workflow, activated it, and the webhook "
        "is reachable. Pass any params; you'll get them back."
    ),
    params_schema={
        "type": "object",
        "additionalProperties": True,
        "description": "Any JSON object — it will be echoed back.",
    },
    invoke=None,  # n8n-backed — dispatched by connectors._invoke_n8n
    requires_auth=True,        # need to install the workflow first
    install_hint=(
        "Installs a tiny webhook workflow into your n8n instance. No external "
        "auth needed — this is just to prove the wiring works."
    ),
    backend="n8n",
    version="1.0",
    tags=["test", "n8n", "diagnostic"],
    n8n_workflow_template=_WORKFLOW_TEMPLATE,
    n8n_webhook_path="yorik-echo",
))
