"""Bank categories: stored with their German names (bank_sync.py assigns
them; the Finance UI translates them through its catalogue). The model
sees English names, and an English category in a request is matched
against the stored German one."""
from __future__ import annotations

from typing import Optional

EN = {
    "Lebensmittel": "Groceries", "Wohnen": "Housing", "Versicherung": "Insurance",
    "Telekommunikation": "Telecom", "Verträge & Abos": "Contracts & subscriptions", "Auto": "Car",
    "Freizeit": "Leisure", "Gesundheit": "Health", "Einkommen": "Income", "Sonstiges": "Other",
    "unkategorisiert": "Uncategorised",
}
_BY_EN = {v.lower(): k for k, v in EN.items()}
_BY_EN.update({"subscriptions": "Verträge & Abos", "contracts": "Verträge & Abos", "food": "Lebensmittel",
               "uncategorized": "unkategorisiert", "rent": "Wohnen"})


def english(stored: Optional[str]) -> Optional[str]:
    return EN.get(stored, stored) if stored else stored


def stored(asked: Optional[str]) -> Optional[str]:
    """The stored name for what the model asked for (English or German)."""
    if not asked:
        return asked
    return _BY_EN.get(asked.strip().lower(), asked)
