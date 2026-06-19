#!/usr/bin/env python3
"""Volume seeder for Yorik test installs.

Generates a meaningful test corpus (~80 documents, ~200 photos,
~100 contacts) and pushes it into a running Paperless / Immich /
Yorik install. Two-phase by design so dev VMs don't keep burning the
Unsplash API quota:

  1. fetch   — Generate / download into data/seed-cache/. Idempotent;
               re-runs skip what's already there. Workstation runs
               this once; cache dir can then be rsync'd to fresh VMs.
  2. seed    — Upload the cached files into the running services.
               Idempotent on the receiving side (Paperless dedups by
               checksum, Immich by hash, contacts by channel-unique).

Cache location: data/seed-cache/{docs,photos,contacts}/
   data/* is gitignored already, so the cache won't bloat the repo.

Usage:
    bash scripts/seed-test-data.sh fetch all      # workstation, once
    bash scripts/seed-test-data.sh seed all       # against a running VM
    bash scripts/seed-test-data.sh status         # what's cached, what's seeded

Per-type:
    bash scripts/seed-test-data.sh fetch docs     # synthetic German PDFs
    bash scripts/seed-test-data.sh fetch photos   # Unsplash (needs API key)
    bash scripts/seed-test-data.sh fetch contacts # Faker .vcf
    bash scripts/seed-test-data.sh seed docs
    bash scripts/seed-test-data.sh seed photos
    bash scripts/seed-test-data.sh seed contacts
    bash scripts/seed-test-data.sh seed calendar     # fake events involving contacts
    bash scripts/seed-test-data.sh seed whatsapp     # fake chat history for some contacts

Environment:
    UNSPLASH_ACCESS_KEY    — get free dev key at https://unsplash.com/developers
    YORIK_SEED_CONTACTS    — number of contacts to generate (default 100)
    YORIK_SEED_PHOTOS      — number of photos to fetch (default 200)
    YORIK_SEED_EVENTS      — number of calendar events to generate (default 80)
    YORIK_SEED_WA_CHATS    — number of contacts to give chat history (default 15)
"""
from __future__ import annotations

import argparse
import json
import os
import random
import shutil
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

# ── Repo root + cache layout ─────────────────────────────────────────

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / "data" / "seed-cache"
DOC_CACHE = CACHE / "docs"
PHOTO_CACHE = CACHE / "photos"
CONTACT_CACHE = CACHE / "contacts"
CALENDAR_CACHE = CACHE / "calendar"
WHATSAPP_CACHE = CACHE / "whatsapp"

# Make the repo importable so we can reach credential_store + connectors
sys.path.insert(0, str(ROOT))


# ── Dependency check (faker + reportlab + requests) ─────────────────

def _ensure_deps() -> None:
    """Install missing pip deps into the local venv on first run.
    Keeps the script self-contained — devs don't have to read this
    file's imports + manually pip-install."""
    missing: List[str] = []
    for mod, pkg in [("faker", "faker"), ("reportlab", "reportlab"),
                     ("requests", "requests"), ("PIL", "Pillow")]:
        try:
            __import__(mod)
        except ImportError:
            missing.append(pkg)
    if not missing:
        return
    print(f"→ installing missing deps: {', '.join(missing)}")
    import subprocess
    subprocess.run([sys.executable, "-m", "pip", "install", "--quiet", *missing],
                   check=True)


# ── Synthetic German doc generation (no external download) ──────────

def _gen_invoice_pdf(out: Path, fake, kind: str = "rechnung") -> None:
    """Generate a ~1-page German-flavored PDF that exercises the
    autotagger's taxonomy.

    Kind is one of: rechnung / nebenkosten / mahnung / mietvertrag /
    arztrechnung / kfz_versicherung / bescheid / werkstatt /
    arbeitsvertrag / kontoauszug. Different kinds use different
    layouts + vocabulary so the LLM picks distinct taxonomy tags."""
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.pdfgen import canvas

    c = canvas.Canvas(str(out), pagesize=A4)
    w, h = A4

    sender_name = fake.company() if random.random() > 0.3 else fake.name()
    sender_street = fake.street_address()
    sender_city = f"{fake.postcode()} {fake.city()}"
    recipient_name = fake.name() if random.random() > 0.4 else fake.company()
    recipient_street = fake.street_address()
    recipient_city = f"{fake.postcode()} {fake.city()}"
    today = fake.date_between(start_date="-3y", end_date="today")
    doc_number = f"{random.randint(1000,99999)}-{today.year}"
    amount = round(random.uniform(15, 2400), 2)

    # Sender block
    c.setFont("Helvetica", 9)
    c.drawString(20*mm, h - 25*mm, sender_name)
    c.drawString(20*mm, h - 30*mm, sender_street)
    c.drawString(20*mm, h - 35*mm, sender_city)

    # Recipient block
    c.setFont("Helvetica", 10)
    c.drawString(20*mm, h - 60*mm, recipient_name)
    c.drawString(20*mm, h - 65*mm, recipient_street)
    c.drawString(20*mm, h - 70*mm, recipient_city)

    # Date + doc number
    c.drawRightString(w - 20*mm, h - 60*mm, f"{sender_city}, {today.strftime('%d.%m.%Y')}")

    # Title + body — varies per kind
    c.setFont("Helvetica-Bold", 14)
    if kind == "rechnung":
        c.drawString(20*mm, h - 95*mm, f"Rechnung Nr. {doc_number}")
        c.setFont("Helvetica", 10)
        c.drawString(20*mm, h - 110*mm, f"Kundennummer: K-{random.randint(10000, 99999)}")
        c.drawString(20*mm, h - 120*mm, f"Leistungsdatum: {today.strftime('%d.%m.%Y')}")
        c.drawString(20*mm, h - 140*mm,
                     f"Wir berechnen Ihnen für die erbrachte Leistung folgenden Betrag:")
        c.setFont("Helvetica-Bold", 12)
        c.drawString(20*mm, h - 160*mm, f"Gesamtbetrag: {amount:.2f} EUR")
        c.setFont("Helvetica", 10)
        c.drawString(20*mm, h - 180*mm,
                     f"Zahlbar innerhalb 14 Tagen auf das Konto IBAN {fake.iban()}.")
        c.drawString(20*mm, h - 195*mm, f"Bei Rückfragen wenden Sie sich an uns unter "
                                          f"{fake.email()}.")
    elif kind == "nebenkosten":
        c.drawString(20*mm, h - 95*mm, "Nebenkostenabrechnung")
        c.setFont("Helvetica", 10)
        c.drawString(20*mm, h - 110*mm, f"Abrechnungszeitraum: 01.01.{today.year-1} – 31.12.{today.year-1}")
        c.drawString(20*mm, h - 130*mm, f"Heizkosten:     {round(amount*0.4,2)} EUR")
        c.drawString(20*mm, h - 140*mm, f"Wasser/Abwasser: {round(amount*0.2,2)} EUR")
        c.drawString(20*mm, h - 150*mm, f"Müll:           {round(amount*0.1,2)} EUR")
        c.drawString(20*mm, h - 160*mm, f"Hausverwaltung: {round(amount*0.3,2)} EUR")
        c.setFont("Helvetica-Bold", 11)
        c.drawString(20*mm, h - 175*mm, f"Gesamt: {amount:.2f} EUR")
    elif kind == "mahnung":
        c.drawString(20*mm, h - 95*mm, "Zahlungserinnerung — 1. Mahnung")
        c.setFont("Helvetica", 10)
        c.drawString(20*mm, h - 110*mm,
                     f"Bezüglich Rechnung Nr. {doc_number} vom {today.strftime('%d.%m.%Y')}")
        c.drawString(20*mm, h - 130*mm,
                     f"Trotz unserer Rechnung ist der Betrag von {amount:.2f} EUR")
        c.drawString(20*mm, h - 140*mm, "bislang nicht auf unserem Konto eingegangen.")
        c.drawString(20*mm, h - 160*mm,
                     "Wir bitten um Begleichung innerhalb der nächsten 10 Tage, andernfalls")
        c.drawString(20*mm, h - 170*mm,
                     "müssen wir Mahngebühren in Höhe von 5,00 EUR berechnen.")
    elif kind == "mietvertrag":
        c.drawString(20*mm, h - 95*mm, "Mietvertrag über Wohnraum")
        c.setFont("Helvetica", 10)
        c.drawString(20*mm, h - 115*mm, f"Vermieter: {sender_name}, {sender_street}")
        c.drawString(20*mm, h - 125*mm, f"Mieter: {recipient_name}, {recipient_street}")
        c.drawString(20*mm, h - 145*mm, f"Mietobjekt: {fake.street_address()}, "
                                         f"{fake.postcode()} {fake.city()}")
        c.drawString(20*mm, h - 165*mm, f"Mietbeginn: {today.strftime('%d.%m.%Y')}")
        c.drawString(20*mm, h - 175*mm, f"Kaltmiete:    {round(amount*0.7,2)} EUR / Monat")
        c.drawString(20*mm, h - 185*mm, f"Nebenkosten:  {round(amount*0.3,2)} EUR / Monat")
        c.drawString(20*mm, h - 195*mm, f"Kaution:      {round(amount*3,2)} EUR (3 Monatsmieten)")
    elif kind == "arztrechnung":
        c.drawString(20*mm, h - 95*mm, f"Arztrechnung — {sender_name}")
        c.setFont("Helvetica", 10)
        c.drawString(20*mm, h - 115*mm,
                     f"Patient: {recipient_name}, geb. {fake.date_of_birth(minimum_age=20, maximum_age=80).strftime('%d.%m.%Y')}")
        c.drawString(20*mm, h - 125*mm, f"Behandlungsdatum: {today.strftime('%d.%m.%Y')}")
        c.drawString(20*mm, h - 145*mm, "Erbrachte Leistungen (GOÄ):")
        c.drawString(20*mm, h - 160*mm,
                     f"  Ziffer 1 — Beratung       {round(amount*0.2,2):>8} EUR")
        c.drawString(20*mm, h - 170*mm,
                     f"  Ziffer 5 — Untersuchung   {round(amount*0.5,2):>8} EUR")
        c.drawString(20*mm, h - 180*mm,
                     f"  Ziffer 250 — Labor        {round(amount*0.3,2):>8} EUR")
        c.setFont("Helvetica-Bold", 11)
        c.drawString(20*mm, h - 200*mm, f"Gesamt: {amount:.2f} EUR")
    elif kind == "kfz_versicherung":
        c.drawString(20*mm, h - 95*mm, "KFZ-Versicherungspolice")
        c.setFont("Helvetica", 10)
        c.drawString(20*mm, h - 115*mm, f"Versicherungsnehmer: {recipient_name}")
        c.drawString(20*mm, h - 125*mm,
                     f"Fahrzeug: {random.choice(['VW Golf', 'BMW 3er', 'Mercedes A-Klasse', 'Audi A4', 'Skoda Octavia'])} "
                     f"({random.randint(2010, 2023)})")
        c.drawString(20*mm, h - 135*mm,
                     f"Kennzeichen: {random.choice(['B','H','HH','M','K','F'])}-{random.choice('ABCDEFGHIJKLMNOPQRSTUVWXYZ')}{random.choice('ABCDEFGHIJKLMNOPQRSTUVWXYZ')} {random.randint(100,9999)}")
        c.drawString(20*mm, h - 155*mm,
                     f"Tarif: {random.choice(['Haftpflicht', 'Teilkasko', 'Vollkasko'])}")
        c.drawString(20*mm, h - 165*mm, f"Beitrag jährlich: {amount:.2f} EUR")
        c.drawString(20*mm, h - 175*mm,
                     f"Vertragsbeginn: {today.strftime('%d.%m.%Y')}")
    elif kind == "bescheid":
        c.drawString(20*mm, h - 95*mm, f"Bescheid — Az. {doc_number}")
        c.setFont("Helvetica", 10)
        c.drawString(20*mm, h - 115*mm, f"Behörde: {sender_name}")
        c.drawString(20*mm, h - 135*mm,
                     "Sehr geehrte Damen und Herren,")
        c.drawString(20*mm, h - 150*mm,
                     "anbei erhalten Sie den Bescheid in oben genannter Angelegenheit.")
        c.drawString(20*mm, h - 170*mm,
                     f"Festgesetzte Höhe: {amount:.2f} EUR")
        c.drawString(20*mm, h - 185*mm,
                     "Die Rechtsbehelfsbelehrung finden Sie auf der Rückseite.")
    elif kind == "werkstatt":
        c.drawString(20*mm, h - 95*mm, f"Werkstattrechnung — {sender_name}")
        c.setFont("Helvetica", 10)
        c.drawString(20*mm, h - 115*mm, f"Fahrzeug: VW Golf, "
                                         f"Kennzeichen B-{random.choice('ABCDEFG')}{random.choice('ABCDEFG')} {random.randint(100,9999)}")
        c.drawString(20*mm, h - 125*mm, f"Werkstatt-Auftrag {doc_number}")
        c.drawString(20*mm, h - 145*mm, "Durchgeführte Arbeiten:")
        c.drawString(20*mm, h - 160*mm, "  Inspektion 60.000 km gem. Herstellervorgabe")
        c.drawString(20*mm, h - 170*mm, "  Ölwechsel + Filterwechsel")
        c.drawString(20*mm, h - 180*mm, "  Bremsbeläge vorne ersetzt")
        c.setFont("Helvetica-Bold", 11)
        c.drawString(20*mm, h - 200*mm, f"Gesamt brutto: {amount:.2f} EUR")
    elif kind == "arbeitsvertrag":
        c.drawString(20*mm, h - 95*mm, "Arbeitsvertrag")
        c.setFont("Helvetica", 10)
        c.drawString(20*mm, h - 115*mm, f"Arbeitgeber: {sender_name}, {sender_street}")
        c.drawString(20*mm, h - 125*mm, f"Arbeitnehmer: {recipient_name}, {recipient_street}")
        c.drawString(20*mm, h - 145*mm,
                     f"Tätigkeit: {random.choice(['Softwareentwickler', 'Buchhalterin', 'Projektmanager', 'Vertriebsassistentin'])}")
        c.drawString(20*mm, h - 155*mm, f"Beginn: {today.strftime('%d.%m.%Y')}")
        c.drawString(20*mm, h - 165*mm,
                     f"Bruttogehalt: {round(amount*30, 2):.2f} EUR / Monat")
        c.drawString(20*mm, h - 175*mm,
                     f"Wochenarbeitszeit: 40 Stunden")
        c.drawString(20*mm, h - 195*mm, "Probezeit: 6 Monate gemäß § 622 BGB")
    elif kind == "kontoauszug":
        c.drawString(20*mm, h - 95*mm, f"Kontoauszug Nr. {doc_number}")
        c.setFont("Helvetica", 10)
        c.drawString(20*mm, h - 115*mm, f"Konto-Inhaber: {recipient_name}")
        c.drawString(20*mm, h - 125*mm, f"IBAN: {fake.iban()}")
        c.drawString(20*mm, h - 145*mm, f"Anfangssaldo: {round(amount*5, 2):.2f} EUR")
        for i, line in enumerate(range(8)):
            ts = today - timedelta(days=i*3)
            amt = round(random.uniform(-300, 500), 2)
            sign = "-" if amt < 0 else "+"
            c.drawString(20*mm, h - (160 + i*8)*mm,
                         f"{ts.strftime('%d.%m.')} {fake.company():<30} {sign}{abs(amt):>8.2f} EUR")
    c.showPage()
    c.save()


def fetch_docs() -> None:
    DOC_CACHE.mkdir(parents=True, exist_ok=True)
    from faker import Faker
    fake = Faker("de_DE")
    Faker.seed(42)  # deterministic so re-runs don't pile up new files
    random.seed(42)
    # Distribution across taxonomy categories so the autotagger has
    # broad coverage to exercise. Numbers tuned to ~80 docs total.
    plan = [
        ("rechnung", 25),
        ("nebenkosten", 6),
        ("mahnung", 5),
        ("mietvertrag", 4),
        ("arztrechnung", 10),
        ("kfz_versicherung", 6),
        ("bescheid", 8),
        ("werkstatt", 6),
        ("arbeitsvertrag", 4),
        ("kontoauszug", 6),
    ]
    total = sum(n for _, n in plan)
    existing = len(list(DOC_CACHE.glob("*.pdf")))
    if existing >= total:
        print(f"  ✓ {existing} docs already cached at {DOC_CACHE} (skipping)")
        return
    print(f"  → generating {total} German-flavored PDFs into {DOC_CACHE}")
    idx = 0
    for kind, count in plan:
        for n in range(count):
            idx += 1
            out = DOC_CACHE / f"{idx:03d}_{kind}.pdf"
            if out.exists():
                continue
            _gen_invoice_pdf(out, fake, kind=kind)
    print(f"  ✓ {idx} PDFs written")


# ── Unsplash photo download ─────────────────────────────────────────

UNSPLASH_CATEGORIES = [
    ("family",      30),
    ("food",        25),
    ("landscape",   30),
    ("portrait",    25),
    ("office",      15),
    ("garden",      15),
    ("wedding",     15),
    ("travel",      20),
    ("birthday",    15),
    ("kids",        10),
]


def fetch_photos() -> None:
    PHOTO_CACHE.mkdir(parents=True, exist_ok=True)
    target_total = int(os.environ.get("YORIK_SEED_PHOTOS", 200))
    existing = list(PHOTO_CACHE.glob("*.jpg"))
    if len(existing) >= target_total:
        print(f"  ✓ {len(existing)} photos already cached at {PHOTO_CACHE} (skipping)")
        return

    key = os.environ.get("UNSPLASH_ACCESS_KEY", "").strip()
    if not key:
        # Fallback to Lorem Picsum — no API key, deterministic via seed,
        # gives Immich's CLIP indexer something to chew on. Subjects are
        # random landscapes/objects so semantic search ("nature", "outdoor")
        # works; precise-subject searches won't.
        import requests
        print(f"  no UNSPLASH_ACCESS_KEY — falling back to Lorem Picsum")
        print(f"  → downloading {target_total} random photos into {PHOTO_CACHE}")
        for i in range(target_total):
            out = PHOTO_CACHE / f"picsum_{i:04d}.jpg"
            if out.exists():
                continue
            try:
                r = requests.get(f"https://picsum.photos/seed/yorik-{i}/1024/768",
                                 timeout=20)
                r.raise_for_status()
                out.write_bytes(r.content)
            except Exception as exc:  # noqa: BLE001
                print(f"    ! picsum {i} failed: {exc}")
                continue
            if (i + 1) % 25 == 0:
                print(f"    · {i+1}/{target_total}")
            time.sleep(0.05)
        print(f"  ✓ Picsum photos downloaded")
        return

    import requests
    print(f"  → fetching ~{target_total} photos from Unsplash into {PHOTO_CACHE}")
    print(f"    (free dev tier = 50 req/h; this needs ~10 search calls + 200 image downloads)")

    downloaded = 0
    for category, want in UNSPLASH_CATEGORIES:
        if downloaded >= target_total:
            break
        try:
            r = requests.get(
                "https://api.unsplash.com/search/photos",
                params={"query": category, "per_page": want, "page": 1,
                        "orientation": "landscape" if category in ("landscape", "travel") else "squarish"},
                headers={"Authorization": f"Client-ID {key}"},
                timeout=15,
            )
            r.raise_for_status()
            results = r.json().get("results", [])
        except Exception as exc:  # noqa: BLE001
            print(f"    ! Unsplash search '{category}' failed: {exc}")
            continue
        for photo in results:
            if downloaded >= target_total:
                break
            url = photo.get("urls", {}).get("regular")
            if not url:
                continue
            pid = photo.get("id", "")
            out = PHOTO_CACHE / f"{category}_{pid}.jpg"
            if out.exists():
                downloaded += 1
                continue
            try:
                ir = requests.get(url, timeout=30)
                ir.raise_for_status()
                out.write_bytes(ir.content)
                downloaded += 1
            except Exception as exc:  # noqa: BLE001
                print(f"    ! download {pid} failed: {exc}")
            time.sleep(0.1)  # polite to the CDN
        print(f"    · {category}: +{want} → {downloaded} total")
    print(f"  ✓ {downloaded} photos downloaded")


# ── Faker contact generation ────────────────────────────────────────

def fetch_contacts() -> None:
    CONTACT_CACHE.mkdir(parents=True, exist_ok=True)
    target = int(os.environ.get("YORIK_SEED_CONTACTS", 100))
    out = CONTACT_CACHE / "seed.vcf"
    if out.exists() and out.stat().st_size > 0:
        # Count VCARDs in the existing file
        existing = out.read_text().count("BEGIN:VCARD")
        if existing >= target:
            print(f"  ✓ {existing} contacts already cached at {out} (skipping)")
            return

    from faker import Faker
    fake = Faker("de_DE")
    Faker.seed(7)
    random.seed(7)
    print(f"  → generating {target} German-locale contacts into {out}")

    relations = ["family", "friend", "colleague", "vendor", "customer",
                 "service_provider", "neighbor"]

    def _vcard_for_person(idx: int) -> str:
        first = fake.first_name()
        last = fake.last_name()
        email = f"{first.lower()}.{last.lower()}@{fake.free_email_domain()}"
        phone = fake.phone_number()
        # Strip non-digits + add German country code for WA-jid potential
        digits = "".join(c for c in phone if c.isdigit())
        if digits.startswith("0"): digits = "49" + digits[1:]
        bday = fake.date_of_birth(minimum_age=20, maximum_age=75)
        street = fake.street_address()
        city = fake.city()
        postcode = fake.postcode()
        rel = random.choice(relations)
        lines = [
            "BEGIN:VCARD",
            "VERSION:3.0",
            f"FN:{first} {last}",
            f"N:{last};{first};;;",
            f"EMAIL;TYPE=INTERNET:{email}",
            f"TEL;TYPE=CELL:+{digits}",
            f"BDAY:{bday.strftime('%Y%m%d')}",
            f"ADR;TYPE=HOME:;;{street};{city};;{postcode};Germany",
            f"NOTE:{rel}",
            "END:VCARD",
        ]
        return "\n".join(lines)

    def _vcard_for_business(idx: int) -> str:
        co = fake.company()
        domain = co.lower().split()[0].replace(",", "") + ".de"
        email = f"info@{domain}"
        phone = fake.phone_number()
        digits = "".join(c for c in phone if c.isdigit())
        if digits.startswith("0"): digits = "49" + digits[1:]
        street = fake.street_address()
        city = fake.city()
        postcode = fake.postcode()
        iban = fake.iban()
        tax = f"DE{random.randint(100000000, 999999999)}"
        lines = [
            "BEGIN:VCARD",
            "VERSION:3.0",
            f"FN:{co}",
            f"ORG:{co}",
            f"EMAIL;TYPE=WORK:{email}",
            f"TEL;TYPE=WORK:+{digits}",
            f"ADR;TYPE=WORK:;;{street};{city};;{postcode};Germany",
            f"NOTE:vendor · IBAN {iban} · USt-ID {tax}",
            "END:VCARD",
        ]
        return "\n".join(lines)

    cards: List[str] = []
    n_business = int(target * 0.3)
    n_person = target - n_business
    for i in range(n_person):
        cards.append(_vcard_for_person(i))
    for i in range(n_business):
        cards.append(_vcard_for_business(i))
    random.shuffle(cards)
    out.write_text("\n\n".join(cards) + "\n", encoding="utf-8")
    print(f"  ✓ {target} contacts written ({n_person} persons, {n_business} businesses)")


# ── Calendar events: fake meetings/appointments tied to contacts ────

def _parse_seed_contacts() -> List[Dict[str, str]]:
    """Re-parse the seeded vCard file into lightweight dicts (name +
    phone + relation). Used by calendar + whatsapp seeders so events
    and chats actually reference real seeded contact names — gives the
    contact enricher cross-source data to mine."""
    src = CONTACT_CACHE / "seed.vcf"
    if not src.exists():
        return []
    parsed: List[Dict[str, str]] = []
    current: Dict[str, str] = {}
    for line in src.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line == "BEGIN:VCARD":
            current = {}
        elif line == "END:VCARD":
            if current.get("name"):
                parsed.append(current)
            current = {}
        elif line.startswith("FN:"):
            current["name"] = line[3:].strip()
        elif line.startswith("TEL"):
            # Extract digits from the value part after the last ":"
            digits = "".join(c for c in line.split(":")[-1] if c.isdigit())
            if digits:
                current["phone_digits"] = digits
        elif line.startswith("NOTE:"):
            current["note"] = line[5:].strip()
        elif line.startswith("ORG:"):
            current["is_business"] = "1"
    return parsed


CAL_KINDS = [
    # (label, weight, has_person, note_template, is_recurring)
    ("Arzttermin {person}",            10, True,  "Dr. {person} · Praxis {street}, {city}", False),
    ("Werkstatt — {person}",            6, True,  "Auto bringen zum Service, {street} {city}", False),
    ("Geburtstag {person}",            12, True,  "Geburtstag — denk an Karte!", True),
    ("Mittagessen mit {person}",        8, True,  "Treffen um 13 Uhr bei {place}", False),
    ("Termin Steuerberater {person}",   3, True,  "Steuerunterlagen mitnehmen", False),
    ("Kaffee mit {person}",             6, True,  "Bei {place}, Eichendorffstr. 12", False),
    ("Friseur",                         4, False, "{place}", False),
    ("Yoga",                            5, False, "wöchentlich", True),
    ("Elternabend",                     3, False, "Schule, Aula", False),
    ("Spielplatz mit Kindern",          4, False, "Stadtpark", False),
    ("Werkstatt — TÜV",                 2, False, "TÜV-Termin, Auto Service", False),
    ("Einkaufen",                       3, False, "Wocheneinkauf REWE", False),
    ("Geschäftsessen — {person}",       3, True,  "Restaurant {place}, Reservierung auf 19 Uhr", False),
    ("Besuch bei {person}",             4, True,  "Adresse: {street}, {postcode} {city}", False),
]


def fetch_calendar() -> None:
    CALENDAR_CACHE.mkdir(parents=True, exist_ok=True)
    out = CALENDAR_CACHE / "seed.json"
    target = int(os.environ.get("YORIK_SEED_EVENTS", 80))
    if out.exists():
        try:
            existing = len(json.loads(out.read_text()))
            if existing >= target:
                print(f"  ✓ {existing} events already cached at {out} (skipping)")
                return
        except Exception:
            pass

    from faker import Faker
    fake = Faker("de_DE")
    Faker.seed(17)
    random.seed(17)

    contacts = _parse_seed_contacts()
    if not contacts:
        print("  ! contacts cache empty — run `fetch contacts` first for richer events")
        person_names = [fake.first_name() + " " + fake.last_name() for _ in range(20)]
    else:
        # Bias toward person-typed contacts for "Termin mit X" events
        person_names = [c["name"] for c in contacts
                        if not c.get("is_business")][:30] or [c["name"] for c in contacts]

    places = ["Café Klatsch", "Restaurant Aladdin", "Trattoria Bella",
              "Schillerstr. 4", "Marktplatz 1", "Stadthalle", "im Park"]

    # Build the weighted kind list once for cheap sampling
    weighted_kinds = []
    for kind in CAL_KINDS:
        weighted_kinds.extend([kind] * kind[1])

    events: List[Dict[str, Any]] = []
    print(f"  → generating {target} calendar events into {out}")
    today = datetime.now()
    for _ in range(target):
        label, _w, has_person, note_template, is_recurring = random.choice(weighted_kinds)
        # Spread across past 18 months + next 6 months
        offset_days = random.randint(-540, 180)
        starts = today + timedelta(days=offset_days)
        # Round to a plausible time-of-day
        hour = random.choice([8, 9, 10, 11, 14, 15, 16, 17, 18, 19])
        starts = starts.replace(hour=hour, minute=random.choice([0, 15, 30, 45]),
                                  second=0, microsecond=0)
        ends = starts + timedelta(hours=random.choice([1, 1, 1, 2]))

        person = random.choice(person_names) if has_person else None
        title = label.replace("{person}", person or "")
        # Note often contains an address — perfect enricher fodder
        note = note_template.format(
            person=person or "",
            street=fake.street_address(),
            city=fake.city(),
            postcode=fake.postcode(),
            place=random.choice(places),
        ).strip()

        events.append({
            "title":     title,
            "starts_at": starts.strftime("%Y-%m-%d %H:%M:%S"),
            "ends_at":   ends.strftime("%Y-%m-%d %H:%M:%S"),
            "all_day":   0,
            "person":    person,
            "notes":     note,
            "recurring": "yearly" if is_recurring and "Geburtstag" in label
                         else ("weekly" if is_recurring else None),
        })

    out.write_text(json.dumps(events, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  ✓ {len(events)} events written ({sum(1 for e in events if e['person'])} carry a person ref)")


def seed_calendar() -> None:
    src = CALENDAR_CACHE / "seed.json"
    if not src.exists():
        print(f"  ✗ no cached calendar at {src} — run `fetch calendar` first")
        return
    try:
        events = json.loads(src.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        print(f"  ✗ couldn't parse {src}: {exc}")
        return
    if not events:
        print("  ✗ cache empty")
        return
    print(f"  → inserting {len(events)} events into the events table")
    try:
        import sqlite3
        from backend.database import DEFAULT_DB_PATH
    except Exception as exc:  # noqa: BLE001
        print(f"  ✗ backend import failed: {exc}")
        return
    db = os.environ.get("HOMEOS_DB_PATH") or str(DEFAULT_DB_PATH)
    # Resolve relative path against repo root the same way backend does
    if not os.path.isabs(db):
        db = str(ROOT / db)
    inserted = skipped = 0
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        for ev in events:
            # Dedup: same title + starts_at = same event. Cheap UNIQUE-ish.
            existing = conn.execute(
                "SELECT id FROM events WHERE title=? AND starts_at=?",
                (ev["title"], ev["starts_at"]),
            ).fetchone()
            if existing:
                skipped += 1
                continue
            conn.execute(
                "INSERT INTO events (title, starts_at, ends_at, all_day, person, notes, recurring) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (ev["title"], ev["starts_at"], ev["ends_at"], int(ev.get("all_day") or 0),
                 ev.get("person"), ev.get("notes"), ev.get("recurring")),
            )
            inserted += 1
        conn.commit()
    print(f"  ✓ inserted {inserted} · {skipped} already present")


# ── WhatsApp message backfill ───────────────────────────────────────

WA_PATTERNS_CASUAL = [
    "Hey, alles klar?", "Bis später!", "Ja kein Ding", "Klar, passt",
    "Bin gerade unterwegs", "Ruf dich gleich an", "Wann passt's dir?",
    "Klingt gut!", "Danke dir!", "Bis morgen :)", "Was machst du heute?",
    "Bin schon zu Hause", "Auf jeden Fall", "Mache ich",
    "Hab gerade gesehen, super!", "Geil 👍", "Sehe ich auch so",
    "Magst noch?", "Ach echt?", "Hahaha", "Ja stimmt",
    "Sorry, hab's übersehen", "Können wir morgen besprechen?",
    "Bin müde, geh schlafen", "Schlaf gut", "Hab dich lieb",
]

WA_PATTERNS_FORMAL = [
    "Guten Morgen", "Vielen Dank für die Rückmeldung.",
    "Anbei die gewünschten Unterlagen.", "Termin am {date} bestätigt.",
    "Mit freundlichen Grüßen", "Die Rechnung folgt per Post.",
    "Wir melden uns nächste Woche.", "Bitte um kurze Bestätigung.",
    "Wann passt Ihnen ein Termin?", "Verstanden, vielen Dank.",
]

WA_DATE_REFS = [
    "am {date}", "morgen", "übermorgen", "nächste Woche",
    "am Freitag", "am Wochenende", "Samstag Abend",
]


def fetch_whatsapp() -> None:
    WHATSAPP_CACHE.mkdir(parents=True, exist_ok=True)
    out = WHATSAPP_CACHE / "seed.json"
    target_chats = int(os.environ.get("YORIK_SEED_WA_CHATS", 15))
    if out.exists():
        try:
            existing = len(json.loads(out.read_text()))
            if existing >= target_chats:
                print(f"  ✓ {existing} chats already cached at {out} (skipping)")
                return
        except Exception:
            pass

    from faker import Faker
    fake = Faker("de_DE")
    Faker.seed(23)
    random.seed(23)

    contacts = _parse_seed_contacts()
    if not contacts:
        print("  ✗ contacts cache empty — run `fetch contacts` first")
        return
    # Pick contacts with phones; bias toward persons over businesses
    eligible = [c for c in contacts
                if c.get("phone_digits") and not c.get("is_business")]
    if not eligible:
        print("  ✗ no eligible contacts (need phone + non-business)")
        return
    picks = random.sample(eligible, min(target_chats, len(eligible)))

    print(f"  → generating chat history for {len(picks)} contacts into {out}")
    chats: List[Dict[str, Any]] = []
    today = datetime.now()
    for c in picks:
        jid = f"{c['phone_digits']}@s.whatsapp.net"
        # 15-80 messages per chat, spread over up to 9 months
        n_msgs = random.randint(15, 80)
        # Bucket message gaps so most-recent are tighter (matches real
        # chat patterns where you talk daily then go silent for weeks)
        starts_ago_days = random.randint(60, 270)
        msg_times: List[datetime] = []
        cursor = today - timedelta(days=starts_ago_days)
        while len(msg_times) < n_msgs and cursor < today:
            cursor += timedelta(
                hours=random.choice([1, 2, 4, 8, 24, 24, 24, 48, 72, 168]),
            )
            if cursor < today:
                msg_times.append(cursor)
        msg_times.sort()

        relation_is_family = "family" in (c.get("note") or "").lower() or random.random() > 0.6
        pool = WA_PATTERNS_CASUAL if relation_is_family else (
            WA_PATTERNS_CASUAL + WA_PATTERNS_FORMAL)

        messages: List[Dict[str, Any]] = []
        for i, ts in enumerate(msg_times):
            from_me = random.random() > 0.45  # slight bias toward us-sent
            text = random.choice(pool)
            # ~15% messages reference a date — fodder for compose_draft's
            # "I told them I'd come Friday" understanding
            if random.random() < 0.15:
                date_ref = random.choice(WA_DATE_REFS).format(
                    date=fake.date_between(start_date="-7d", end_date="+30d").strftime("%d.%m."))
                text = f"{text} {date_ref}"
            # Occasional longer messages
            if random.random() < 0.1:
                text = text + " " + fake.sentence(nb_words=random.randint(4, 10))
            messages.append({
                "msg_id":   f"seed-{c['phone_digits']}-{i:04d}",
                "from_me":  1 if from_me else 0,
                "push_name": None if from_me else c["name"],
                "timestamp": int(ts.timestamp()),
                "text":     text,
            })

        chats.append({
            "jid":       jid,
            "name":      c["name"],
            "is_group":  0,
            "messages":  messages,
            "last_text": messages[-1]["text"] if messages else "",
            "last_ts":   messages[-1]["timestamp"] if messages else 0,
        })

    out.write_text(json.dumps(chats, ensure_ascii=False, indent=2), encoding="utf-8")
    total_msgs = sum(len(c["messages"]) for c in chats)
    print(f"  ✓ {len(chats)} chats / {total_msgs} messages written")


def seed_whatsapp() -> None:
    src = WHATSAPP_CACHE / "seed.json"
    if not src.exists():
        print(f"  ✗ no cached whatsapp at {src} — run `fetch whatsapp` first")
        return
    try:
        chats = json.loads(src.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        print(f"  ✗ couldn't parse {src}: {exc}")
        return
    if not chats:
        print("  ✗ cache empty")
        return
    print(f"  → inserting {len(chats)} chats + their messages directly into wa_chats / wa_messages")
    try:
        import sqlite3
        from backend.database import DEFAULT_DB_PATH
    except Exception as exc:  # noqa: BLE001
        print(f"  ✗ backend import failed: {exc}")
        return
    db = os.environ.get("HOMEOS_DB_PATH") or str(DEFAULT_DB_PATH)
    if not os.path.isabs(db):
        db = str(ROOT / db)

    n_chats = 0
    n_msgs = 0
    n_dupes = 0
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        for ch in chats:
            jid = ch["jid"]
            # Upsert chat (don't clobber if it exists from prior seeding)
            existing = conn.execute(
                "SELECT jid FROM wa_chats WHERE jid=?", (jid,)
            ).fetchone()
            if not existing:
                conn.execute(
                    "INSERT INTO wa_chats (jid, name, is_group, last_message_ts, "
                    " last_message_text, owner_user_id) VALUES (?, ?, ?, ?, ?, 1)",
                    (jid, ch["name"], int(ch.get("is_group") or 0),
                     int(ch.get("last_ts") or 0), ch.get("last_text") or ""),
                )
                n_chats += 1
            for m in ch["messages"]:
                try:
                    conn.execute(
                        "INSERT INTO wa_messages (msg_id, chat_jid, from_me, "
                        " push_name, timestamp, text, owner_user_id) "
                        "VALUES (?, ?, ?, ?, ?, ?, 1)",
                        (m["msg_id"], jid, int(m.get("from_me") or 0),
                         m.get("push_name"), int(m["timestamp"]), m["text"]),
                    )
                    n_msgs += 1
                except sqlite3.IntegrityError:
                    # (chat_jid, msg_id) is UNIQUE — already seeded
                    n_dupes += 1
        conn.commit()
    print(f"  ✓ inserted {n_chats} new chats · {n_msgs} new messages · {n_dupes} dupes")
    print(f"    Chats appear in /whatsapp; autodraft style mirroring now has examples to learn from")


# ── Seed: upload cached files into running services ────────────────

def _paperless_creds():
    try:
        from backend.connectors.paperless import _settings
        return _settings()
    except Exception as exc:  # noqa: BLE001
        print(f"  ✗ couldn't read Paperless creds: {exc}")
        return {}


def _immich_creds():
    try:
        from backend.connectors.immich import _creds
        return _creds()
    except Exception as exc:  # noqa: BLE001
        print(f"  ✗ couldn't read Immich creds: {exc}")
        return {}


def seed_docs() -> None:
    files = sorted(DOC_CACHE.glob("*.pdf"))
    if not files:
        print(f"  ✗ no cached docs at {DOC_CACHE} — run `fetch docs` first")
        return
    creds = _paperless_creds()
    if not creds.get("api_key"):
        print("  ✗ no Paperless API key — configure in Settings → Connectors first")
        return
    import requests
    base = creds["base_url"].rstrip("/")
    headers = {"Authorization": f"Token {creds['api_key']}"}
    print(f"  → uploading {len(files)} PDFs to Paperless at {base}")
    ok, dup, err = 0, 0, 0
    for f in files:
        with f.open("rb") as fh:
            r = requests.post(
                f"{base}/api/documents/post_document/",
                headers=headers,
                files={"document": (f.name, fh, "application/pdf")},
                timeout=60,
            )
        if r.status_code in (200, 201, 202):
            ok += 1
        elif r.status_code == 409 or "duplicate" in r.text.lower():
            dup += 1
        else:
            err += 1
            print(f"    ! {f.name} → HTTP {r.status_code}: {r.text[:120]}")
        if (ok + dup + err) % 20 == 0:
            print(f"    · {ok + dup + err}/{len(files)}")
    print(f"  ✓ uploaded {ok} · {dup} dupes · {err} errors")
    print(f"    Paperless will OCR + classify asynchronously; check the queue in its UI")


def seed_photos() -> None:
    files = sorted(PHOTO_CACHE.glob("*.jpg"))
    if not files:
        print(f"  ✗ no cached photos at {PHOTO_CACHE} — run `fetch photos` first")
        return
    creds = _immich_creds()
    if not creds.get("api_key"):
        print("  ✗ no Immich API key — configure in Settings → Connectors first")
        return
    import requests
    from datetime import datetime
    base = creds["base_url"].rstrip("/")
    headers = {"x-api-key": creds["api_key"]}
    print(f"  → uploading {len(files)} photos to Immich at {base}")
    ok, dup, err = 0, 0, 0
    for f in files:
        # Spread fake EXIF dates across the last 3 years so date filters
        # in find_photo have something to bite on. Deterministic via path.
        stat = f.stat()
        ts_seed = sum(ord(c) for c in f.name)
        fake_taken = datetime.now() - timedelta(days=ts_seed % 1095)
        with f.open("rb") as fh:
            r = requests.post(
                f"{base}/api/assets",
                headers=headers,
                files={"assetData": (f.name, fh, "image/jpeg")},
                data={
                    "deviceAssetId":  f.name,
                    "deviceId":       "yorik-seeder",
                    "fileCreatedAt":  fake_taken.isoformat() + "Z",
                    "fileModifiedAt": datetime.fromtimestamp(stat.st_mtime).isoformat() + "Z",
                },
                timeout=60,
            )
        if r.status_code in (200, 201):
            body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
            if body.get("duplicate"):
                dup += 1
            else:
                ok += 1
        else:
            err += 1
            print(f"    ! {f.name} → HTTP {r.status_code}: {r.text[:120]}")
        if (ok + dup + err) % 25 == 0:
            print(f"    · {ok + dup + err}/{len(files)}")
    print(f"  ✓ uploaded {ok} · {dup} dupes · {err} errors")
    print(f"    Immich will run face detection + smart-search indexing in the background")


def seed_contacts() -> None:
    src = CONTACT_CACHE / "seed.vcf"
    if not src.exists():
        print(f"  ✗ no cached contacts at {src} — run `fetch contacts` first")
        return
    # Direct call into backend.contacts_import — same plan/apply pipeline
    # the vCard import modal uses, just without the HTTP wrap. Avoids
    # needing an authenticated session for a CLI seeder.
    raw = src.read_text(encoding="utf-8")
    print(f"  → importing {raw.count('BEGIN:VCARD')} contacts via backend.contacts_import")
    try:
        from backend import contacts_import
    except Exception as exc:  # noqa: BLE001
        print(f"  ✗ couldn't import contacts_import: {exc}")
        return
    cards = contacts_import.parse_vcards(raw)
    print(f"    parsed {len(cards)} vCards")
    plan = contacts_import.plan_import(cards)
    new_count = sum(1 for e in plan.entries if e.outcome == "new")
    merge_count = sum(1 for e in plan.entries if e.outcome == "merge")
    conflict_count = sum(1 for e in plan.entries if e.outcome == "name_conflict")
    print(f"    plan: {new_count} new · {merge_count} merge · {conflict_count} name-conflict")
    result = contacts_import.apply_import(plan, target_status="active", user_id=1)
    print(f"  ✓ created {len(result.created_ids)} · merged {len(result.merged_ids)} · "
          f"skipped {result.skipped}"
          + (f" · {len(result.errors)} errors" if result.errors else ""))
    if result.errors:
        for e in result.errors[:5]:
            print(f"    ! {e.get('display_name')}: {e.get('error')}")


# ── Status ───────────────────────────────────────────────────────────

def cmd_status() -> None:
    print(f"Cache root: {CACHE}")
    for name, p in [
        ("docs",     DOC_CACHE),
        ("photos",   PHOTO_CACHE),
        ("contacts", CONTACT_CACHE),
        ("calendar", CALENDAR_CACHE),
        ("whatsapp", WHATSAPP_CACHE),
    ]:
        if not p.exists():
            print(f"  {name:10s} — (not fetched)")
            continue
        if name == "contacts":
            f = p / "seed.vcf"
            n = f.read_text().count("BEGIN:VCARD") if f.exists() else 0
            print(f"  {name:10s} — {n} vCards" + (f"  ({f})" if n else ""))
        elif name in ("calendar", "whatsapp"):
            f = p / "seed.json"
            n = 0
            if f.exists():
                try:
                    data = json.loads(f.read_text())
                    n = len(data) if isinstance(data, list) else 0
                except Exception:
                    pass
            label = "events" if name == "calendar" else "chats"
            print(f"  {name:10s} — {n} {label}" + (f"  ({f})" if n else ""))
        else:
            glob = "*.pdf" if name == "docs" else "*.jpg"
            files = list(p.glob(glob))
            print(f"  {name:10s} — {len(files)} files  ({p})")


# ── CLI ──────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(description="Seed Yorik test corpora")
    sub = parser.add_subparsers(dest="cmd", required=True)
    choices = ["docs", "photos", "contacts", "calendar", "whatsapp", "all"]
    fetch_p = sub.add_parser("fetch", help="Populate the local cache")
    fetch_p.add_argument("what", choices=choices, default="all", nargs="?")
    seed_p = sub.add_parser("seed", help="Upload cached items into running services")
    seed_p.add_argument("what", choices=choices, default="all", nargs="?")
    sub.add_parser("status", help="Show cache state")

    args = parser.parse_args()
    _ensure_deps()

    if args.cmd == "status":
        cmd_status()
        return 0

    actions = {
        "fetch": {
            "docs":     fetch_docs,
            "photos":   fetch_photos,
            "contacts": fetch_contacts,
            "calendar": fetch_calendar,
            "whatsapp": fetch_whatsapp,
        },
        "seed": {
            "docs":     seed_docs,
            "photos":   seed_photos,
            "contacts": seed_contacts,
            "calendar": seed_calendar,
            "whatsapp": seed_whatsapp,
        },
    }[args.cmd]

    # `all` is ordered so dependencies are satisfied — contacts must
    # exist before calendar/whatsapp seeders can reference them.
    if args.what == "all":
        targets = ["docs", "photos", "contacts", "calendar", "whatsapp"]
    else:
        targets = [args.what]
    for t in targets:
        print(f"\n[{args.cmd} {t}]")
        actions[t]()
    return 0


if __name__ == "__main__":
    sys.exit(main())
