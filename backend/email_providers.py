"""Provider auto-detect for common email services.

Given a user's email address, we infer the right IMAP + SMTP host/port
config so the account-add flow can show pre-filled, working defaults
instead of asking the user to look up server names. They can always
override for generic IMAP/Exchange setups.

If a domain isn't in the table, we return a "generic" template they
can fill out manually. Adding new providers = one entry.
"""

from __future__ import annotations
from typing import Optional


# Each preset: imap_host/port/ssl, smtp_host/port/ssl/starttls,
# and notes the user might need (e.g. "Gmail requires app password").
PROVIDERS: dict[str, dict] = {
    "gmail.com": {
        "name": "Gmail",
        "imap_host": "imap.gmail.com", "imap_port": 993, "imap_ssl": True,
        "smtp_host": "smtp.gmail.com", "smtp_port": 465, "smtp_ssl": True, "smtp_starttls": False,
        "notes": "Requires a Google **app password** (Account → Security → 2-Step Verification → App passwords). Your normal Google password will be rejected by IMAP.",
        "docs_url": "https://support.google.com/accounts/answer/185833",
    },
    "googlemail.com": "gmail.com",  # alias

    "outlook.com": {
        "name": "Outlook / Hotmail",
        "imap_host": "outlook.office365.com", "imap_port": 993, "imap_ssl": True,
        "smtp_host": "smtp.office365.com", "smtp_port": 587, "smtp_ssl": False, "smtp_starttls": True,
        "notes": "If you have 2FA enabled, generate an app password under Microsoft account → Security.",
        "docs_url": "https://support.microsoft.com/en-us/account-billing/manage-app-passwords-for-two-step-verification-d6dc8c6d-4bf7-4851-ad95-6d07799387e9",
    },
    "hotmail.com":  "outlook.com",
    "live.com":     "outlook.com",
    "msn.com":      "outlook.com",
    "office365.com": "outlook.com",

    "icloud.com": {
        "name": "iCloud Mail",
        "imap_host": "imap.mail.me.com", "imap_port": 993, "imap_ssl": True,
        "smtp_host": "smtp.mail.me.com", "smtp_port": 587, "smtp_ssl": False, "smtp_starttls": True,
        "notes": "iCloud requires an **app-specific password** (appleid.apple.com → Sign-In and Security → App-Specific Passwords).",
        "docs_url": "https://support.apple.com/en-us/HT204397",
    },
    "me.com":   "icloud.com",
    "mac.com":  "icloud.com",

    "yahoo.com": {
        "name": "Yahoo Mail",
        "imap_host": "imap.mail.yahoo.com", "imap_port": 993, "imap_ssl": True,
        "smtp_host": "smtp.mail.yahoo.com", "smtp_port": 465, "smtp_ssl": True, "smtp_starttls": False,
        "notes": "Yahoo requires an app password (Account Security → Generate app password).",
        "docs_url": "https://help.yahoo.com/kb/SLN15241.html",
    },
    "ymail.com": "yahoo.com",

    "gmx.de": {
        "name": "GMX",
        "imap_host": "imap.gmx.net", "imap_port": 993, "imap_ssl": True,
        "smtp_host": "mail.gmx.net", "smtp_port": 465, "smtp_ssl": True, "smtp_starttls": False,
        "notes": "Make sure POP3/IMAP access is enabled in GMX Einstellungen → POP3/IMAP-Zugriff.",
    },
    "gmx.net":  "gmx.de",
    "gmx.com":  "gmx.de",
    "gmx.at":   "gmx.de",
    "gmx.ch":   "gmx.de",

    "web.de": {
        "name": "WEB.DE",
        "imap_host": "imap.web.de", "imap_port": 993, "imap_ssl": True,
        "smtp_host": "smtp.web.de", "smtp_port": 587, "smtp_ssl": False, "smtp_starttls": True,
        "notes": "POP3/IMAP must be enabled in Einstellungen → POP3/IMAP-Abruf. Password is the regular WEB.DE password.",
    },

    "t-online.de": {
        "name": "Telekom (t-online.de)",
        "imap_host": "secureimap.t-online.de", "imap_port": 993, "imap_ssl": True,
        "smtp_host": "securesmtp.t-online.de", "smtp_port": 465, "smtp_ssl": True, "smtp_starttls": False,
        "notes": "Use the 'Passwort für E-Mail-Programme' that Telekom assigned, not your Kundencenter password.",
    },

    "ionos.de": {
        "name": "IONOS",
        "imap_host": "imap.ionos.de", "imap_port": 993, "imap_ssl": True,
        "smtp_host": "smtp.ionos.de", "smtp_port": 465, "smtp_ssl": True, "smtp_starttls": False,
        "notes": "Use your full email address as username + the mailbox password (NOT your IONOS account login).",
    },
    "1und1.de": "ionos.de",
    "1and1.com": "ionos.de",

    "fastmail.com": {
        "name": "Fastmail",
        "imap_host": "imap.fastmail.com", "imap_port": 993, "imap_ssl": True,
        "smtp_host": "smtp.fastmail.com", "smtp_port": 465, "smtp_ssl": True, "smtp_starttls": False,
        "notes": "Generate an app password in Settings → Privacy & Security → Integrations.",
    },

    "mailbox.org": {
        "name": "mailbox.org",
        "imap_host": "imap.mailbox.org", "imap_port": 993, "imap_ssl": True,
        "smtp_host": "smtp.mailbox.org", "smtp_port": 465, "smtp_ssl": True, "smtp_starttls": False,
        "notes": "Use your full mailbox.org address + your mailbox password.",
    },

    "posteo.de": {
        "name": "Posteo",
        "imap_host": "posteo.de", "imap_port": 993, "imap_ssl": True,
        "smtp_host": "posteo.de", "smtp_port": 465, "smtp_ssl": True, "smtp_starttls": False,
        "notes": "Use your full Posteo address + password.",
    },

    "proton.me": {
        "name": "Proton Mail",
        # Bridge serves both endpoints with STARTTLS (plaintext socket
        # upgraded to TLS via the STARTTLS command). NOT implicit SSL —
        # connecting with ssl=True yields "WRONG VERSION NUMBER".
        "imap_host": "127.0.0.1", "imap_port": 1143, "imap_ssl": False, "imap_starttls": True,
        "smtp_host": "127.0.0.1", "smtp_port": 1025, "smtp_ssl": False, "smtp_starttls": True,
        "notes": "Proton Mail only exposes IMAP/SMTP through the **Proton Mail Bridge** running locally. Install Bridge, sign in there, then enter the host/port + token Bridge shows you.",
        # Tells the frontend to surface a prominent alert BEFORE the
        # password field — without it users try their Proton web
        # password (which Bridge accounts reject silently).
        "bridge_required": True,
        "bridge_steps": [
            "Download & install Proton Mail Bridge from proton.me/mail/bridge (requires a paid Proton plan).",
            "Sign into Bridge with your Proton account.",
            "In Bridge, select your account → Mailbox details → copy the SMTP/IMAP password Bridge generated for you.",
            "Paste THAT password below (not your Proton web password).",
        ],
        "docs_url": "https://proton.me/mail/bridge",
    },
    "protonmail.com": "proton.me",
    "pm.me":          "proton.me",
}


def lookup_provider(email: str) -> Optional[dict]:
    """Return the preset for `email`'s domain, or None for unknown."""
    if not email or "@" not in email:
        return None
    domain = email.split("@", 1)[1].lower().strip()
    preset = PROVIDERS.get(domain)
    # Follow aliases (string values).
    while isinstance(preset, str):
        preset = PROVIDERS.get(preset)
    return preset


def generic_preset() -> dict:
    """Fallback for unknown domains — leaves hosts blank, sane defaults."""
    return {
        "name": "Generic IMAP / SMTP",
        "imap_host": "", "imap_port": 993, "imap_ssl": True, "imap_starttls": False,
        "smtp_host": "", "smtp_port": 465, "smtp_ssl": True, "smtp_starttls": False,
        "notes": "Enter your provider's IMAP and SMTP server settings. Most modern services use port 993 for IMAP (SSL) and 465 (SSL) or 587 (STARTTLS) for SMTP.",
    }
