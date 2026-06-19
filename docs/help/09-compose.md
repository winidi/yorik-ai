---
title: Compose — letters, invoices, offers
nav_app: compose
summary: Write proper German postal letters, invoices, offers from templates. Multi-recipient contact pickers, paste/upload extraction, ZUGFeRD-compliant invoice PDFs.
---

# Compose — letters, invoices, offers

The Compose app generates PDF documents from declarative templates. Bundled templates cover the high-frequency German cases: Kündigung Mietvertrag, allgemeine Vertragskündigung, Mietminderung, Brief allgemein, generische E-Mail, Rechnung. Community templates extend the catalog.

## Quick start

1. Compose app → **Templates** tab (sidebar).
2. Pick a template.
3. Fill in the args panel (left side). Required fields marked.
4. Editor (right side) shows the live preview as you type.
5. **Save** stores as a draft. **Download PDF** renders to file. **Send** opens the send dialog (email / print / etc.).

## Picking a contact for the recipient

Each recipient slot has a small **From contacts** button. Click → search → pick. The contact's name + postal address fill the form. Yorik never invents addresses.

If the template has multiple contact slots (employer + HR on a resignation letter, Vermieter + Hausverwaltung on a complaint), each slot has its OWN picker. Picking one doesn't fill the others.

## Paste / upload for fast fill

Each recipient slot also has **paste** and **upload** buttons next to the contact picker:

- **Paste**: dump any text (a copied email signature, a website blob, a snipped letter header). Yorik's LLM extracts the matching fields for THIS slot only, ignores noise.
- **Upload**: drop a PDF / Word / .txt file. Yorik runs OCR (via Paperless's tesseract) → same extraction.

Useful when you have a contact-card text but the person isn't in your contacts hub yet.

## Letter chrome

The bundled letter templates produce DIN 5008-shaped output: sender block, Anschriftenfeld, date, Betreff, Sehr geehrte Damen und Herren, body, Mit freundlichen Grüßen, signature, footer.

You don't write the chrome — the template does. You only fill the slots (recipient, body text, dates, references).

## Invoices

The `rechnung-de` template emits a modern German B2B invoice — sender block, Von/An, Leistungen table, Zwischensumme/USt/Gesamtbetrag, payment block. With the ZUGFeRD extension installed (Settings → Extensions), the PDF carries an embedded `factur-x.xml` payload — readable by DATEV / Lexware / sevDesk.

Invoice numbering is automatic from your Rechnungs-Serie (Settings → Numbering → install German preset). The next number reserves on preview, consumes on save. Audit log captures every allocation.

## Writing a draft from chat

Tell Yorik:

- *"Schreib eine Kündigung des Mietvertrags an [name] zum [datum]"* — Yorik finds the right template, asks for the missing fields, opens the draft.
- *"Erstell eine Rechnung an Mustermann GmbH für 2 Stunden Beratung zu 150 € netto"* — opens a Rechnung draft pre-filled.

After the LLM creates the draft, you finish it in Compose. The LLM doesn't render PDFs itself; it just sets up the args panel for you.

## Auto-subject + "Write with AI" buttons

Each long-content field (body, subject) has a small **AI** button. Click → modal where you tell Yorik what the field should say in plain language. Yorik writes it from your intent, considering the rest of the form. Useful when you know the gist but not the exact words.

Subject fields have a one-click **Auto** button that generates a subject from the current body — no modal needed.

## Custom templates

Drop your own template JSON in `templates/` (yorik-ai repo) or contribute via PR to yorik-community. See `docs/TEMPLATE_AUTHORING.md` in yorik-community for the rendering quirks.
