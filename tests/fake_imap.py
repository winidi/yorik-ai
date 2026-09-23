"""A small in-memory IMAP server with the surface of imapclient.IMAPClient
that Yorik uses: folders with UIDVALIDITY/UIDNEXT, UID SEARCH (ALL,
UNDELETED, UID sets, SINCE), FETCH of the items the fetcher asks for,
flags, MOVE/COPY/EXPUNGE with and without MOVE and UIDPLUS.

A test keeps one FakeServer as "the mailbox on the server" and hands out
FakeClient connections to it, the way GMX would."""

from __future__ import annotations

import itertools
import re
from datetime import date, datetime, timedelta, timezone
from email.utils import format_datetime

_mid = itertools.count(1)


def mail(subject: str = "Hallo", *, days_old: float = 0, plain: bool = True, html: bool = True,
         message_id: str | None = None, sender: str = "anna@example.org") -> tuple[bytes, datetime]:
    """(raw RFC 822 bytes, internal date) — multipart/alternative with a
    text and an HTML part by default, like most mail."""
    when = datetime.now(timezone.utc) - timedelta(days=days_old)
    mid = message_id or f"m{next(_mid)}@example.org"
    head = (f"From: Anna <{sender}>\r\nTo: beate@example.test\r\nSubject: {subject}\r\n"
            f"Date: {format_datetime(when)}\r\nMessage-ID: <{mid}>\r\nMIME-Version: 1.0\r\n")
    if plain and html:
        body = ("Content-Type: multipart/alternative; boundary=\"b1\"\r\n\r\n"
                "--b1\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n"
                f"{subject} als Text\r\n"
                "--b1\r\nContent-Type: text/html; charset=utf-8\r\n\r\n"
                f"<p>{subject} als HTML</p>\r\n--b1--\r\n")
    elif plain:
        body = f"Content-Type: text/plain; charset=utf-8\r\n\r\n{subject} als Text\r\n"
    else:
        body = f"Content-Type: text/html; charset=utf-8\r\n\r\n<p>{subject} als HTML</p>\r\n"
    return (head + body).encode(), when


class Folder:
    def __init__(self, name: str, flags=(), validity: int = 1000):
        self.name, self.flags, self.validity = name, tuple(flags), validity
        self.next_uid = 1
        self.msgs: dict[int, dict] = {}

    def add(self, raw: bytes, when: datetime, flags=(), uid: int | None = None) -> int:
        uid = uid or self.next_uid
        self.next_uid = max(self.next_uid, uid + 1)
        self.msgs[uid] = {"raw": raw, "date": when, "flags": set(flags)}
        return uid


class FakeServer:
    def __init__(self, *, move: bool = True, uidplus: bool = True, idle: bool = True):
        self.caps = {b"IMAP4REV1"} | ({b"MOVE"} if move else set()) | ({b"UIDPLUS"} if uidplus else set()) \
            | ({b"IDLE"} if idle else set())
        self.folders: dict[str, Folder] = {
            "INBOX": Folder("INBOX", (b"\\HasNoChildren",)),
            "Archiv": Folder("Archiv", (b"\\Archive", b"\\HasNoChildren")),
            "Gelöscht": Folder("Gelöscht", (b"\\Trash", b"\\HasNoChildren")),
        }
        self.plain_expunges = 0          # a plain EXPUNGE is what must never happen

    def deliver(self, folder: str, subject: str = "Hallo", **kw) -> int:
        raw, when = mail(subject, **kw)
        return self.folders[folder].add(raw, when)

    def client(self) -> "FakeClient":
        return FakeClient(self)


class FakeClient:
    def __init__(self, server: FakeServer):
        self.s = server
        self.selected: Folder | None = None
        self.readonly = False

    # connection
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def login(self, *_a):
        return b"OK"

    def logout(self):
        return b"BYE"

    def starttls(self, **_k):
        return None

    # IDLE returns at once: a test wants the pass, not the wait.
    def idle(self):
        return None

    def idle_check(self, timeout=None):
        return []

    def idle_done(self):
        return (b"OK", [])

    def has_capability(self, cap):
        return (cap if isinstance(cap, bytes) else cap.encode()) in self.s.caps

    # folders
    def list_folders(self):
        return [(f.flags, b"/", f.name) for f in self.s.folders.values()]

    def select_folder(self, name, readonly=False):
        self.selected = self.s.folders[name]
        self.readonly = readonly
        return {b"EXISTS": len(self.selected.msgs), b"UIDVALIDITY": self.selected.validity}

    def folder_status(self, name, what=None):
        f = self.s.folders[name]
        return {b"UIDVALIDITY": f.validity, b"UIDNEXT": f.next_uid, b"MESSAGES": len(f.msgs),
                b"UNSEEN": sum(1 for m in f.msgs.values() if b"\\Seen" not in m["flags"])}

    # search
    def search(self, criteria):
        f = self.selected
        crit = list(criteria)
        uids = set(f.msgs)
        i = 0
        while i < len(crit):
            c = crit[i]
            if c == "ALL":
                pass
            elif c == "UNDELETED":
                uids = {u for u in uids if b"\\Deleted" not in f.msgs[u]["flags"]}
            elif c == "UID":
                uids &= self._uidset(str(crit[i + 1]), f)
                i += 1
            elif c == "SINCE":
                d = crit[i + 1]
                d = d if isinstance(d, date) else date.fromisoformat(str(d))
                uids = {u for u in uids if f.msgs[u]["date"].date() >= d}
                i += 1
            else:
                raise AssertionError(f"fake IMAP: unknown search key {c!r}")
            i += 1
        return sorted(uids)

    @staticmethod
    def _uidset(spec: str, f: Folder) -> set:
        out: set = set()
        top = max(f.msgs) if f.msgs else 0
        for part in spec.split(","):
            if ":" in part:
                a, b = part.split(":")
                lo = int(a)
                hi = top if b == "*" else int(b)
                if b == "*" and lo > top:          # RFC 3501: n:* always includes the highest
                    out.add(top) if top else None
                out |= set(range(lo, hi + 1))
            else:
                out.add(int(part))
        return out

    # fetch
    def fetch(self, uids, items):
        f = self.selected
        items = [i if isinstance(i, bytes) else i.encode() for i in items]
        out = {}
        for u in uids:
            m = f.msgs.get(u)
            if m is None:
                continue
            head, _, body = m["raw"].partition(b"\r\n\r\n")
            d = {b"SEQ": u}
            for it in items:
                if it == b"INTERNALDATE":
                    d[it] = m["date"]
                elif it == b"FLAGS":
                    d[it] = tuple(sorted(m["flags"]))
                elif it == b"RFC822.SIZE":
                    d[it] = len(m["raw"])
                elif it == b"BODY.PEEK[HEADER]":
                    d[b"BODY[HEADER]"] = head + b"\r\n\r\n"
                elif it == b"BODY.PEEK[TEXT]":
                    d[b"BODY[TEXT]"] = body
                elif it.startswith(b"BODY.PEEK[HEADER.FIELDS"):
                    key = it.replace(b"BODY.PEEK", b"BODY")
                    mid = re.search(rb"Message-ID: (<[^>]+>)", head)
                    d[key] = (b"Message-ID: " + mid.group(1) + b"\r\n\r\n") if mid else b"\r\n"
                else:
                    raise AssertionError(f"fake IMAP: unknown fetch item {it!r}")
            out[u] = d
        return out

    # changes
    def _writable(self):
        assert not self.readonly, "fake IMAP: change in a folder opened read-only"

    def add_flags(self, uids, flags):
        self._writable()
        for u in uids:
            if u in self.selected.msgs:
                self.selected.msgs[u]["flags"] |= set(flags)

    def remove_flags(self, uids, flags):
        self._writable()
        for u in uids:
            if u in self.selected.msgs:
                self.selected.msgs[u]["flags"] -= set(flags)

    def delete_messages(self, uids):
        self.add_flags(uids, [b"\\Deleted"])

    def copy(self, uids, target):
        t = self.s.folders[target]
        for u in uids:
            m = self.selected.msgs[u]
            t.add(m["raw"], m["date"], m["flags"] - {b"\\Deleted"})

    def move(self, uids, target):
        assert b"MOVE" in self.s.caps, "fake IMAP: MOVE without the capability"
        self._writable()
        self.copy(uids, target)
        for u in uids:
            self.selected.msgs.pop(u, None)

    def uid_expunge(self, uids):
        assert b"UIDPLUS" in self.s.caps, "fake IMAP: UID EXPUNGE without UIDPLUS"
        self._writable()
        for u in uids:
            if b"\\Deleted" in self.selected.msgs.get(u, {}).get("flags", set()):
                self.selected.msgs.pop(u)

    def expunge(self, messages=None):
        self._writable()
        if messages is not None:
            return self.uid_expunge(messages)
        self.s.plain_expunges += 1
        for u in [u for u, m in self.selected.msgs.items() if b"\\Deleted" in m["flags"]]:
            self.selected.msgs.pop(u)
