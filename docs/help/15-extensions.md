---
title: Extensions — ZUGFeRD + regional add-ons
nav_app: settings
nav_query:
  tab: letters
summary: "Optional Python modules. Today: ZUGFeRD/Factur-X for German e-invoicing. Admins install it under Settings › Letters & invoices › E-invoices and other extras; no restart needed."
---

# Extensions — ZUGFeRD + regional add-ons

Extensions are optional Python modules that add capabilities the base install doesn't carry — typically locale-specific or compliance-driven, where most users don't need the weight.

Today: one extension ships — **ZUGFeRD / Factur-X**. It embeds a CrossIndustryInvoice XML payload inside the invoice PDFs that Write (and Compose) make, producing a PDF/A-3 that DATEV / Lexware / sevDesk parse automatically. Required for German B2B invoicing 2025–2028.

## Installing

**Settings › Letters & invoices › E-invoices and other extras** (admins only) → click **Install** next to ZUGFeRD.

Yorik runs `pip install -r extensions/zugferd/requirements.txt` (drafthorse + factur-x) into the active venv. Takes ~30–90 seconds. The extension is loaded right after, into the running Yorik: the status goes from "dependencies not installed" to "active". No restart.

From then on, invoices finished in the Write app become e-invoices, and any Compose invoice template with `"zugferd": true` gets the XML embed.

## How invoice templates opt in

The bundled `rechnung-de` template ships with `"zugferd": true` + a complete `invoice_fields` Jinja mapping. Plain letter templates leave the flag unset, so they render as regular PDFs untouched.

If you write your own invoice template: see `docs/TEMPLATE_AUTHORING.md` in the yorik-community repo for the `invoice_fields` mapping schema.

## Verifying

After install and saving an invoice (in Write, or in Compose with `rechnung-de`):

- Yorik's logs include `factur-x.xml file added to PDF document`.
- The downloaded PDF contains an attached `factur-x.xml`. Verify via Adobe Acrobat (Attachments panel), `unzip -p invoice.pdf factur-x.xml`, or any PDF reader with attachment support.
- The XML starts with `<rsm:CrossIndustryInvoice ...>` and contains seller / buyer / lines / totals matching the rendered visible PDF.

## What goes wrong

- **Pip install fails**: the entry under Settings › Letters & invoices › E-invoices and other extras shows the error; the full output is in the logs (Settings › Developer › Logs). Usually a network issue. Retry.
- **Hook registered but XML missing**: the template doesn't have `"zugferd": true` or its `invoice_fields` mapping is malformed. Validate with `python scripts/validate-template.py templates/<name>.json`.
- **XML present but DATEV rejects**: probably a field mapping issue. The bundled template passes the official Mustangproject validator (BASIC + EN16931 profile). Custom templates need similar diligence.

## Other extensions on the roadmap

Spec'd but not yet shipped:

- **Peppol Access Point** — push B2G invoices directly to authorities.
- **XRechnung profile** (3.x state-extension variants — Bayern / Berlin / Bremen).
- **eIDAS / D-Trust signature** — for legally-signed PDFs.

None of these are needed for typical B2B invoicing. Open an issue if your use case requires one.
