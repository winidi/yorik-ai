"""Gmail connector — n8n-backed, OAuth2.

The workflow template ships with two operations behind a Switch node:
    {op: "send", to, subject, body}                  → Gmail Send node
    {op: "list_recent", limit?, q?}                  → Gmail List node

OAuth setup (one-time, in n8n's UI after install_connector imports the workflow):
  1. Open the imported workflow in n8n (link appears in the install modal).
  2. Click either Gmail node → Credentials → "Create new credential".
  3. Pick "Gmail OAuth2 API". n8n shows the redirect URL it needs in your
     Google Cloud Console OAuth credential.
  4. In Google Cloud Console:
     - Create OAuth client (Web application)
     - Add the n8n redirect URL to "Authorized redirect URIs"
     - Copy Client ID + Secret back into the n8n credential modal
     - Click "Sign in with Google" inside the credential modal → Allow.
  5. n8n stores the refresh token; from then on Yorik just calls the webhook.

If you want a less-fiddly path: use the `email-imap` connector with a Gmail
app password instead. Gmail still supports app passwords if 2FA is on.
"""

from __future__ import annotations

import json
from pathlib import Path

from . import ConnectorSpec, register

_TEMPLATE_PATH = Path(__file__).resolve().parent.parent.parent / "connector_templates" / "gmail.workflow.json"

with open(_TEMPLATE_PATH) as f:
    _WORKFLOW_TEMPLATE = json.load(f)


register(ConnectorSpec(
    name="email-gmail",
    description=(
        "Send + read Gmail messages via OAuth2. Operations: "
        "{op: 'send', to, subject, body} and {op: 'list_recent', limit}. "
        "Uses n8n's OAuth flow — no passwords stored in Yorik."
    ),
    params_schema={
        "type": "object",
        "required": ["op"],
        "properties": {
            "op":      {"type": "string", "enum": ["send", "list_recent"]},
            "to":      {"type": "string", "description": "Recipient email (for send)"},
            "subject": {"type": "string"},
            "body":    {"type": "string"},
            "limit":   {"type": "integer", "minimum": 1, "maximum": 50, "default": 10},
        },
    },
    invoke=None,  # n8n-backed
    requires_auth=True,
    install_hint=(
        "After import, open the workflow in n8n and configure the Gmail nodes with "
        "OAuth2 (Google Cloud Console redirect URI required). See the connector README "
        "for the step-by-step. Alternative: install email-imap and use a Gmail app password."
    ),
    backend="n8n",
    version="1.0",
    tags=["email", "gmail", "oauth", "n8n"],
    n8n_workflow_template=_WORKFLOW_TEMPLATE,
    n8n_webhook_path="yorik-gmail",
))
