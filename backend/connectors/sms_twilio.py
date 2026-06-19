"""SMS via Twilio — n8n-backed.

Single operation: {op: "send", to, message, from?} → Twilio Send SMS node.

Twilio credential setup (one-time in n8n's UI after install):
  1. Open the imported "Yorik · Twilio SMS" workflow in n8n.
  2. Click the Twilio Send node → Credentials → "Create new credential" →
     "Twilio API".
  3. Paste your Account SID + Auth Token from
     https://console.twilio.com/us1/account/keys-credentials/api-keys
  4. Save. From then on every webhook trigger uses those creds.

`from` can be omitted in the body if you configure a default From number
on the Twilio node in n8n (recommended — saves you typing it every time).
"""

from __future__ import annotations

import json
from pathlib import Path

from . import ConnectorSpec, register

_TEMPLATE_PATH = Path(__file__).resolve().parent.parent.parent / "connector_templates" / "twilio.workflow.json"

with open(_TEMPLATE_PATH) as f:
    _WORKFLOW_TEMPLATE = json.load(f)


register(ConnectorSpec(
    name="sms-twilio",
    description=(
        "Send an SMS via Twilio. {op: 'send', to: '+49...', message: '...'}. "
        "Optionally include 'from' to override the default sender. Requires a "
        "Twilio account (free trial available)."
    ),
    params_schema={
        "type": "object",
        "required": ["op", "to", "message"],
        "properties": {
            "op":      {"type": "string", "enum": ["send"]},
            "to":      {"type": "string", "description": "E.164 phone number, e.g. '+491701234567'"},
            "message": {"type": "string", "description": "SMS body (160-char chunks; n8n handles splitting)"},
            "from":    {"type": "string", "description": "E.164 sender. Default set on the Twilio node in n8n."},
        },
    },
    invoke=None,
    requires_auth=True,
    install_hint=(
        "After import, configure the Twilio credential in n8n (Account SID + Auth Token "
        "from console.twilio.com). Set a default From number on the Twilio node to avoid "
        "passing it on every call."
    ),
    backend="n8n",
    version="1.0",
    tags=["sms", "twilio", "messaging", "n8n"],
    n8n_workflow_template=_WORKFLOW_TEMPLATE,
    n8n_webhook_path="yorik-twilio",
))
