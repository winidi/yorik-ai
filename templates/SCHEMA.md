# Compose Template Schema

A Yorik compose template is a single JSON file under `templates/`. This
doc describes the fields the frontend + backend look at, and the
**declarative roles** that drive the AI write buttons, the
contacts picker, the body-no-chrome prompt rule, and any future
automatic behavior keyed off "what is this field for."

Run `python3 scripts/validate-template.py templates/your-file.json`
before opening a PR — it surfaces missing/unknown roles and other
common issues.

## Required top-level fields

| Field | Type | Notes |
|---|---|---|
| `id` | string | Stable identifier, kebab-case (e.g. `kuendigung-mietvertrag-de`). Used in URLs + saved drafts. **Never change after publish** — saved drafts reference it. |
| `name` | string | Human label shown in the template picker. |
| `version` | string | Semver. Bump on any change that affects rendered output. |
| `tags` | string[] | At least one of: `letter`, `email`, `invoice`, `offer`, `memo`. Drives which `kind=` value matches. |
| `default_args` | object | Per-arg default value (use `""` for empty strings — empty triggers the `{% else %}` stub in `body_html`). |
| `ask_user_for_args` | array | Per-arg metadata (label, hint, **role**, input shape). See below. |
| `body_html` | string | Jinja template rendering the document body. References `{{ args.foo }}`. |

## `ask_user_for_args` entry — the important one

Each arg is an object:

```json
{
  "key": "body_text",
  "label": "Brieftext",
  "required": true,
  "input": "textarea",
  "role": "body",
  "hint": "Worum geht es? 1-3 Absätze reichen."
}
```

- **`key`** (required) — matches a `default_args` key.
- **`label`** (recommended) — human label. Used in the AI write modal title, the args panel field labels.
- **`required`** (optional) — affects the readiness chip in the footer.
- **`input`** (optional) — `"text"` (default) or `"textarea"`. The AI write button falls back to `input === "textarea"` when no role is set.
- **`role`** (recommended) — declarative role. See the table below.
- **`contact_group`** (optional) — string label that pairs name + address args belonging to the SAME contact role. Use when a template has multiple distinct recipient contacts (e.g. `Arbeitgeber` + `HR-Abteilung` on a resignation letter, or `Vermieter` + `Hausverwaltung` on a Mietminderung). The Compose UI renders one independent contacts picker per group, so picking a contact for `Arbeitgeber` doesn't fill the `HR-Abteilung` name. **Omit when there's only one recipient role** — single-recipient templates work fine without it (legacy behavior).
- **`hint`** (optional) — placeholder/tooltip shown in the input field.
- **`pattern`** (optional) — regex the value must match (basic client-side validation).

## Roles — what each one unlocks

Set one of these on every arg. Yorik's automatic behaviors key off
`role` first; without it, the system falls back to key-name regex
which works for conventionally-named fields and breaks for novel
naming.

| Role | What it unlocks | When to use |
|---|---|---|
| `body` | AI write button (opens modal: "user types intent → LLM writes prose"). Backend prompt explicitly forbids greeting/closing/signature chrome in the output. | The multi-line prose body of the document. Usually exactly one per template. |
| `subject` | "Auto" button (one click → LLM writes a subject from the body). Backend prompt enforces one-line output, no chrome. | The subject/title/Betreff line. Usually exactly one per template. |
| `greeting` | (Backend prompt rule: "this field is ONLY the greeting, output one line.") | The opening salutation arg (`anrede`). |
| `closing` | (Backend prompt rule: "this field is ONLY the closing phrase.") | The closing arg (`gruss`). |
| `recipient_name` | Contacts picker shown next to the input. Picking a contact fills this + the sibling `recipient_address`. | "To whom?" field. |
| `recipient_address` | Treated as the sibling-fill target when the user picks a contact on a `recipient_name` field. | Recipient postal address (multi-line). |
| `recipient_email`, `recipient_phone` | Reserved — same family as above, future picker integration. | Recipient email / phone. |
| `sender_name`, `sender_address`, `sender_email`, `sender_phone`, `sender_business` | Auto-prefilled from the logged-in user's profile (Settings → Profile). | "From whom?" fields. |
| `date` | (Reserved — future calendar picker integration.) | Any date arg. |
| `reference_number` | (Reserved — future series-allocation hint.) | Contract / customer / invoice number. |
| `currency_amount` | (Reserved — future currency-input formatting.) | Monetary values. |
| `location` | (Reserved — future maps picker.) | Place / city / venue. |
| `freeform_text` | No special behavior. Treated as multi-line prose if `input: "textarea"` is also set. | Any other text-shaped arg where none of the above fit. |
| `freeform_value` | No special behavior. | Any other scalar arg. |

## Multiple recipients on one letter — `contact_group`

For templates with more than one recipient contact (a resignation letter
addressed to `Arbeitgeber` AND `HR-Abteilung`; a Mietminderung addressed
to `Vermieter` AND `Hausverwaltung`), tag each name+address pair with a
`contact_group`:

```json
{
  "ask_user_for_args": [
    { "key": "arbeitgeber_name",    "label": "Arbeitgeber",    "role": "recipient_name",    "contact_group": "arbeitgeber" },
    { "key": "arbeitgeber_adresse", "label": "Arbeitgeber-Adresse", "role": "recipient_address", "contact_group": "arbeitgeber" },
    { "key": "hr_name",             "label": "HR-Abteilung",   "role": "recipient_name",    "contact_group": "hr" },
    { "key": "hr_adresse",          "label": "HR-Adresse",     "role": "recipient_address", "contact_group": "hr" }
  ]
}
```

Compose then shows two independent contacts pickers (labelled
"Arbeitgeber" and "HR-Abteilung"). Picking a contact for one fills only
that group's name + address args — the other slot stays untouched.

Single-recipient templates don't need `contact_group`; omit it and the
behavior is unchanged.

## Body `{% else %}` placeholder convention

For the body part of your template, render a parens-wrapped italic stub
when the arg is empty so the user sees "yes, here's where text goes":

```jinja
{% if args.body_text %}
  {% for para in args.body_text.split('\n\n') %}<p>{{ para }}</p>{% endfor %}
{% else %}
  <p><em>(Hier kommt der Text rein — Yorik schreibt ihn auf Klick oder du tippst selbst.)</em></p>
{% endif %}
```

The frontend's placeholder detector recognizes parens-wrapped italic
prose ≤ 250 chars and switches the "Ask Yorik" selection panel into
"Write text" mode so the stub doesn't get fed back as context.

## `preview_args` — example values for first-render

`preview_args` is an optional object with example values used by the
template picker's preview, and by the Compose editor on first open
before any real args are entered. **Use placeholder names** — German
de-facto convention is `Max Mustermann` / `Erika Musterfrau` at
`Musterstraße 1, 12345 Musterstadt`. English: `Acme Co` / `John Example`.
Never ship real names, addresses, or emails — the validator flags
likely-real PII.

## Validator

```bash
python3 scripts/validate-template.py templates/                # all
python3 scripts/validate-template.py templates/my-template.json  # one
```

Errors block PR merge; warnings are advisory.
