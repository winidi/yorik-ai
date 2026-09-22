"""Make the Immich login the Yorik login (backend/oidc.py).

Writes both sides: the Immich client in Yorik's credential store (with
the exact redirect URIs) and Immich's OAuth settings through its admin
API. Run from the repo root as the service user, then restart Yorik:

    set -a; . ./config.env; set +a
    PYTHONPATH=. venv/bin/python scripts/configure_immich_oauth.py \
        --issuer https://workstation.tailf0bde1.ts.net:8445 \
        --immich https://workstation.tailf0bde1.ts.net:8443

`--issuer` is Yorik's URL as the browser AND the Immich container see
it (checked 2026-09-22: the container reaches the Tailscale URL).
`--immich` is Immich's public URL, where its login page lives.
`--off` switches Immich's OAuth off again.
"""

from __future__ import annotations

import argparse
import sys

import requests

from backend import credential_store, oidc


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--issuer", help="Yorik's public URL, e.g. https://workstation.tailf0bde1.ts.net:8445")
    ap.add_argument("--immich", help="Immich's public URL, e.g. https://workstation.tailf0bde1.ts.net:8443")
    ap.add_argument("--off", action="store_true", help="disable OAuth in Immich")
    ap.add_argument("--no-autolaunch", action="store_true", help="keep Immich's own login page, add a button instead")
    args = ap.parse_args()

    creds = credential_store.get("immich") or {}
    base = (creds.get("base_url") or "http://localhost:2283").rstrip("/")
    headers = {"x-api-key": creds.get("api_key") or "", "Content-Type": "application/json"}
    if not creds.get("api_key"):
        print("no Immich admin key in the credential store", file=sys.stderr)
        return 1
    cfg = requests.get(f"{base}/api/system-config", headers=headers, timeout=20)
    cfg.raise_for_status()
    config = cfg.json()

    if args.off:
        config["oauth"]["enabled"] = False
        config["oauth"]["autoLaunch"] = False
    else:
        if not (args.issuer and args.immich):
            ap.error("--issuer and --immich are required (or --off)")
        issuer = args.issuer.rstrip("/")
        immich = args.immich.rstrip("/")
        client = oidc.ensure_client("immich", [f"{immich}/auth/login", "app.immich:///oauth-callback"])
        config["oauth"].update({
            "enabled": True,
            "issuerUrl": f"{issuer}/.well-known/openid-configuration",
            "clientId": client["client_id"],
            "clientSecret": client["client_secret"],
            "scope": "openid email profile",
            "signingAlgorithm": "RS256",
            "profileSigningAlgorithm": "none",
            "buttonText": "Mit Yorik anmelden",
            "autoRegister": False,          # the accounts exist; Yorik made them
            "autoLaunch": not args.no_autolaunch,
            "mobileOverrideEnabled": True,
            "mobileRedirectUri": f"{immich}/api/oauth/mobile-redirect",
            "storageLabelClaim": "preferred_username",
            "tokenEndpointAuthMethod": "client_secret_post",
        })
        # Immich with its password login off would lock everyone out if
        # Yorik were down; leave the password login on as the fallback.
        config.setdefault("passwordLogin", {})["enabled"] = True
    r = requests.put(f"{base}/api/system-config", headers=headers, json=config, timeout=20)
    if not r.ok:
        print("Immich refused the config:", r.status_code, r.text[:300], file=sys.stderr)
        return 1
    print("Immich OAuth:", "off" if args.off else f"on, issuer {args.issuer}, autoLaunch {not args.no_autolaunch}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
