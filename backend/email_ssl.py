"""SSL context selection for email IMAP/SMTP connections.

For non-loopback hosts (real mail providers on the internet), use the
default verifying context — anything else would silently accept any
cert and is a real security risk for users.

For loopback hosts (127.0.0.1, ::1, localhost, 127.x.x.x), the
traffic never leaves the machine, so cert verification is pointless.
This unblocks local bridges that ship with self-signed certs — most
notably the Proton Mail Bridge (IMAP/SMTP on 127.0.0.1), but also
Mailpit, MailHog, and any other dev SMTP/IMAP relay.

Centralized here so the same loopback policy applies to:
  - email_sender.test_imap / test_smtp        (account-add validation)
  - email_sender.send_email                   (outbound SMTP)
  - email_actions.* (open IMAP for flag changes / move / append)
  - email_fetcher (long-running IMAP IDLE)
"""
from __future__ import annotations

import ipaddress
import ssl


_LOOPBACK_HOSTNAMES = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0"})


def is_loopback(host: str) -> bool:
    """True for hostnames + IPs that resolve to the local machine.
    Conservative — only matches strings we can confirm are loopback;
    `mybridge.local` returns False (could be anywhere on the LAN)."""
    h = (host or "").strip().lower()
    if not h:
        return False
    if h in _LOOPBACK_HOSTNAMES:
        return True
    try:
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        return False


def make_ssl_context(host: str) -> ssl.SSLContext:
    """Verifying context for the open internet, unverified for
    loopback. Same shape returned in both cases so callers don't need
    to branch on host — they just pass `make_ssl_context(host)` and
    get back something they can hand to IMAPClient / smtplib."""
    if is_loopback(host):
        return ssl._create_unverified_context()
    return ssl.create_default_context()
