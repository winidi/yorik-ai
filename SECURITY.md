# Security Policy

## Reporting a vulnerability

**Please don't open a public GitHub Issue for security bugs.** Email instead:

📧 **[hi@yorik.ai](mailto:hi@yorik.ai)**

Use the subject line: `[SECURITY] Yorik: <one-line description>`.

Include:

- A description of the issue
- Steps to reproduce
- Affected version (output of `git rev-parse HEAD`)
- Potential impact
- Any suggested mitigation

## What to expect

| Time | What we do |
|---|---|
| Within 48h | Acknowledge receipt |
| Within 7 days | First triage + severity assessment |
| Within 30 days | Fix released (critical issues faster — typically within 7 days) |
| At disclosure | Public CVE + credit in changelog (unless you prefer to stay anonymous) |

We follow [coordinated disclosure](https://www.cisa.gov/coordinated-vulnerability-disclosure-process): public disclosure happens after a fix is available, with a window to allow users to update.

## Scope

**In scope:**

- The Yorik backend (`backend/`) — authentication, authorization, SQL injection, command injection, path traversal, RCE, etc.
- The Yorik frontend (`frontend-react/`) — XSS, CSRF, broken auth, sensitive data exposure
- `install.sh`, `start.sh`, and `scripts/*.sh` — privilege escalation, arbitrary file write
- The Docker compose stack as Yorik configures it — misconfigurations that expose services unintentionally
- Credential storage (`backend/credential_store.py`)
- The role-gated SQL runner + spaces ACL (`backend/auth.py`, `backend/spaces.py`, `backend/ask.py`)

**Out of scope:**

- Vulnerabilities in upstream projects (Immich, Paperless, n8n, Ollama, etc.) — please report to those projects directly
- Anything requiring physical or admin access to the host machine
- Denial-of-service via excessive resource consumption (self-hosted; user controls their own resources)
- Social engineering of Yorik maintainers

## Hardening recommendations

For users running Yorik in production-ish environments:

- **Don't expose the bare backend port (`:8000`) to the internet.** Put a reverse proxy with TLS in front, or use Tailscale.
- **Use the Docker bundled services on loopback only** (the default).
- **Rotate your `data/.credential_key` if you ever back up to off-site cloud storage.**
- **Run `bash scripts/backup-restore-drill.sh` quarterly** — proves your backups actually restore.
- **Keep `data/family.db` on encrypted disk** (LUKS / FileVault / BitLocker).

## Past disclosures

| Date | Severity | Issue | Fix |
|---|---|---|---|
| — | — | No public disclosures yet (project is in beta) | — |

## Hall of fame

Contributors who responsibly disclosed security issues will be listed here with their consent.
