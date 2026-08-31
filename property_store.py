"""Persisted, editable per-property facts: display name and currency.

The PMS exports' PROPERTY_NAME and CURRENCY fields are used to auto-detect
these the first time a property code is seen, but the mapping itself - keyed
by the stable PROPERTY code - is what the app actually reads from
afterwards. Some sources may only ever carry the code with no name (or no
currency) at all, so both fall back to a safe default until someone fills
them in here, either by hand in Settings or because a later row happens to
include one.

Name and currency are tracked independently (separate *_configured/*_source
pairs) since they can arrive from different exports at different times - the
cancellation export might be the first to name a hotel, the in-house export
might be the first to know its currency.

Backed by the `properties` table (see models.py), but reads are served from
an in-memory cache loaded once at startup and kept in sync on writes - the
same shape the JSON version had. Without that cache, get()/get_currency()
would run a query every time they're called, and they're called once per
reservation on every check page (hundreds to thousands of times) for only a
couple dozen distinct properties - a real N+1 query problem that showed up
as multi-second page loads, not a cost inherent to using a database.
"""

from __future__ import annotations

import re

from db import SessionLocal
from models import PropertyRecord

_HOTEL_NAME_PATTERN = re.compile(r"^Ruby\s+(.+?)\s+Hotel\b", re.IGNORECASE)
DEFAULT_CURRENCY = "EUR"


def _auto_detect_name(property_name: str, code: str) -> tuple[str, bool]:
    """Returns (name, configured). configured=False means this is just the
    code itself, standing in until a real name is known."""
    if not property_name:
        return code, False
    match = _HOTEL_NAME_PATTERN.match(property_name.strip())
    if match:
        return match.group(1).strip(), True
    return property_name.strip(), True


def _row_to_dict(row: PropertyRecord) -> dict:
    return {
        "name": row.name,
        "name_configured": row.name_configured,
        "name_source": row.name_source,
        "currency": row.currency,
        "currency_configured": row.currency_configured,
        "currency_source": row.currency_source,
    }


def _default_entry(code: str) -> dict:
    return {
        "name": code,
        "name_configured": False,
        "name_source": "auto",
        "currency": DEFAULT_CURRENCY,
        "currency_configured": False,
        "currency_source": "auto",
    }


class PropertyStore:
    def __init__(self):
        with SessionLocal() as session:
            self._cache: dict[str, dict] = {
                row.code: _row_to_dict(row) for row in session.query(PropertyRecord).all()
            }

    def ensure_known_names(self, property_names: dict[str, str]) -> None:
        """Make sure every property code seen has an entry, auto-detecting
        its name. An existing auto-detected name that was never real gets
        upgraded if a name shows up later. Manual edits, and codes that
        already have a real detected name, are never touched. A no-op
        DB-wise once every code is already cached with a real name, which is
        the steady-state case on every page load."""
        to_insert: dict[str, dict] = {}
        to_update: dict[str, dict] = {}
        for code, property_name in property_names.items():
            entry = self._cache.get(code)
            if entry is None:
                new_entry = _default_entry(code)
                name, configured = _auto_detect_name(property_name, code)
                new_entry["name"], new_entry["name_configured"] = name, configured
                to_insert[code] = new_entry
            elif entry["name_source"] == "auto" and not entry["name_configured"] and property_name:
                name, configured = _auto_detect_name(property_name, code)
                if configured:
                    to_update[code] = {"name": name, "name_configured": True}

        if not to_insert and not to_update:
            return
        with SessionLocal() as session:
            for code, entry in to_insert.items():
                session.add(PropertyRecord(code=code, **entry))
            for code, changes in to_update.items():
                row = session.get(PropertyRecord, code)
                row.name = changes["name"]
                row.name_configured = changes["name_configured"]
            session.commit()
        for code, entry in to_insert.items():
            self._cache[code] = entry
        for code, changes in to_update.items():
            self._cache[code].update(changes)

    def ensure_known_currencies(self, property_currencies: dict[str, str]) -> None:
        """Same auto-detect/upgrade/never-clobber pattern as names, for the
        3-letter currency code."""
        to_insert: dict[str, dict] = {}
        to_update: dict[str, dict] = {}
        for code, currency in property_currencies.items():
            currency = currency.strip().upper()
            entry = self._cache.get(code)
            if entry is None:
                new_entry = _default_entry(code)
                if currency:
                    new_entry["currency"], new_entry["currency_configured"] = currency, True
                to_insert[code] = new_entry
            elif entry["currency_source"] == "auto" and not entry["currency_configured"] and currency:
                to_update[code] = {"currency": currency, "currency_configured": True}

        if not to_insert and not to_update:
            return
        with SessionLocal() as session:
            for code, entry in to_insert.items():
                # Might already be queued by ensure_known_names in the same
                # batch and not yet flushed - re-check to avoid a duplicate insert.
                if session.get(PropertyRecord, code) is None:
                    session.add(PropertyRecord(code=code, **entry))
            for code, changes in to_update.items():
                row = session.get(PropertyRecord, code)
                row.currency = changes["currency"]
                row.currency_configured = changes["currency_configured"]
            session.commit()
        for code, entry in to_insert.items():
            if code not in self._cache:
                self._cache[code] = entry
        for code, changes in to_update.items():
            self._cache[code].update(changes)

    def get(self, code: str) -> str:
        entry = self._cache.get(code)
        return entry["name"] if entry else code

    def get_currency(self, code: str) -> str:
        entry = self._cache.get(code)
        return entry["currency"] if entry and entry.get("currency") else DEFAULT_CURRENCY

    def all(self) -> dict[str, dict]:
        return dict(self._cache)

    def update(self, code: str, name: str | None = None, currency: str | None = None) -> None:
        with SessionLocal() as session:
            row = session.get(PropertyRecord, code)
            if row is None:
                row = PropertyRecord(code=code, **_default_entry(code))
                session.add(row)
            if name is not None:
                name = name.strip()
                row.name = name or code
                row.name_configured = bool(name)
                row.name_source = "manual"
            if currency is not None:
                currency = currency.strip().upper()
                if currency:
                    row.currency = currency
                    row.currency_configured = True
                    row.currency_source = "manual"
            session.commit()
            self._cache[code] = _row_to_dict(row)
