# German E-Invoicing in Yorik

The German B2B e-invoicing reform is the most consequential regulatory change for small businesses in 2025-2028. Yorik's Compose app is designed around it.

## The regulation timeline

| Date | What's mandatory |
|---|---|
| **1 Jan 2025** | Every German business must be able to **receive** XRechnung / ZUGFeRD e-invoices. PDF-only is no longer accepted. |
| **1 Jan 2027** | Businesses with annual turnover > €800,000 must **issue** e-invoices. |
| **1 Jan 2028** | All B2B issuance must be e-invoice. PDF-only invoices become non-compliant for input tax deduction. |

Source: [BMF Schreiben vom 15.10.2024](https://www.bundesfinanzministerium.de/) (German Federal Ministry of Finance).

## What this means for Kleinunternehmer

If you're a one-person business, freelancer, Handwerker, etc.:

- Today (2026): you likely use Word + PDF, or pay €15-30/mo to Lexoffice / sevDesk / Billomat
- By 2028: PDF-only stops working — your B2B customers can't deduct your invoices as input VAT
- The SaaS options will keep raising prices because they know you have no alternative

Yorik is the alternative. AGPL-3.0, runs on your own PC, generates compliant XRechnung 3.x + ZUGFeRD 2.x output, GoBD-audited.

## How Yorik generates a compliant invoice

### 1. Number from a series

When you create an invoice in Compose, Yorik:

1. Looks up the active series for your business (e.g. `RE-` with year prefix)
2. Reserves the next number via a two-phase preview/consume protocol
3. Stamps the invoice with `RE-2026-001` (configurable format)
4. Writes an audit entry to `numbering_audit`

The two-phase protocol means a number is *reserved* when you preview the invoice and *consumed* when you save/send. If you cancel before saving, the reservation is released — but the audit log keeps a record so a tax auditor can see the gap was intentional, not skipped.

### 2. GoBD-compliant data

GoBD (Grundsätze zur ordnungsmäßigen Führung und Aufbewahrung von Büchern, Aufzeichnungen und Unterlagen in elektronischer Form sowie zum Datenzugriff) requires:

- ✅ **Unalterable** — Yorik stores invoice XML + PDF as content-addressed blobs (SHA-256). The audit log is append-only.
- ✅ **Complete** — every issued invoice has a number, no gaps in the series (or audit-logged justification for gaps).
- ✅ **Timely** — invoices recorded with creation + send timestamps.
- ✅ **Verifiable** — every audit entry references a user_id and a session.
- ✅ **Retrievable for 10 years** — `data/family.db` + `data/documents/` is the corpus. Back it up.

### 3. ZUGFeRD 2.x / XRechnung 3.x XML

Yorik's Compose render pipeline:

1. Takes your TipTap document content + the template metadata (sender, recipient, line items, tax)
2. Generates the visible PDF/A-3 via Gotenberg (Chromium-based)
3. Generates the structured XML (XRechnung 3.x for B2G, ZUGFeRD 2.x EN16931 profile for B2B)
4. Embeds the XML as an attachment inside the PDF/A-3
5. Saves both to `data/documents/`

The result is a single PDF file that:
- A human can open and read like a regular PDF
- A machine (your customer's accounting system) can parse without OCR

### 4. Sending

You can:
- Send via your configured SMTP account (email auto-attaches the PDF)
- Upload to Paperless via Yorik's bidirectional write-through
- Download and send manually
- (Coming) Push via Peppol Access Point for B2G direct delivery

## What's beta-quality right now

Honest disclosure:

- ✅ Numbering series + audit log: production quality
- ✅ ZUGFeRD 2.2 (Comfort / EN16931 profile): production quality, validates against the [Mustangproject validator](https://www.mustangproject.org/)
- ⚠️ XRechnung 3.x for B2G: works for typical cases, but we haven't validated against every state-specific extension (Bayern, Berlin, Bremen all have minor schema additions)
- ⚠️ Multi-VAT-rate invoices (e.g. 7% + 19% on same invoice): works but the UI is awkward
- ⚠️ Reverse charge invoicing (§13b UStG): manual via template, not auto-detected from recipient country
- ❌ Kleinunternehmerregelung §19 UStG note: must be added manually to the template (TODO: auto-add when business profile is `is_kleinunternehmer=true`)

**Beta testers especially welcome from German Steuerberater (tax advisors)** — if you can validate Yorik's output against your client's actual accounting system, please [open a Discussion](https://github.com/winidi/yorik-ai/discussions).

## Configuration

In `Settings → Numbering`:

- **Series name** (e.g. "Rechnung", "Angebot", "Auftragsbestätigung")
- **Prefix template** (e.g. `RE-{year}-{seq:04d}` → `RE-2026-0001`)
- **Starting sequence number** — if you migrate from Lexoffice mid-year, set this to your next expected number
- **Reset cadence** — yearly (typical), monthly, or never

In your `user_profiles` row (set via onboarding Step 4):

- `business_name`, `tax_id` (USt-IdNr), `iban` — flow into every generated invoice's sender block

## Migrating from Lexoffice / sevDesk

There's no automated importer yet (this is on the roadmap). For now:

1. Export your customers / vendors as CSV from Lexoffice → import via Yorik's Settings → Connectors (coming) or directly into `contacts` table
2. Set your starting Rechnungsnummer in Settings → Numbering to "next number after my last Lexoffice invoice"
3. Keep Lexoffice for a quarter in parallel — re-issue any invoice in both systems and compare PDFs
4. After a quarter of clean output, drop the Lexoffice subscription

Conservative migration is the right approach when €30/mo SaaS is being replaced by AGPL software you compile yourself.

## Background reading

- [BMF FAQ zur verpflichtenden elektronischen Rechnung](https://www.bundesfinanzministerium.de/) (DE, official)
- [XRechnung 3.x specification](https://xeinkauf.de/xrechnung/) (KoSIT, the spec authority)
- [ZUGFeRD 2.x specification](https://www.ferd-net.de/standards/zugferd-2.3/) (FeRD)
- [GoBD 2024 update](https://www.bundesfinanzministerium.de/) — Bundesfinanzministerium

## Questions

GitHub Discussions, tag `e-invoicing`: <https://github.com/winidi/yorik-ai/discussions>
