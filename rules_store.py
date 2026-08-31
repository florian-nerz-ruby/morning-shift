"""Persisted, editable cancellation-policy rules.

The PMS export's CANCELLATION_RULE text (e.g. "1st N 7D Prior") is the source
of truth for a policy — multiple rate codes can and do share the same policy
text. Rules are keyed by a normalized form of that text so casing quirks in
the export ("NRF Full" vs "NRF FULL") don't create duplicate entries.

A rule is:
  fee_type            "first_night" | "full_stay"
  always_late         True for non-refundable rates with no free window at all
  days_before_arrival  free-cancellation threshold in days (None if always_late)
  cutoff_time          "HH:MM" time of day the deadline falls at (e.g. "18:00").
                       Applies to the deadline day regardless of how many days
                       before arrival that is - "1st N 1D Prior" with a cutoff
                       of 18:00 means free until 18:00 the day before arrival,
                       not just until midnight. None if the rule only cares
                       about the date, not the time. Meaningless if always_late.
  configured          False if the text didn't match anything known and no one
                       has configured it yet in Settings
  source              "auto" | "manual"

Backed by the `cancellation_rules` table (see models.py) rather than a JSON
file, but reads are served from an in-memory cache loaded once at startup
and kept in sync on writes - the same shape the JSON version had (load once,
serve from memory, flush on write). Without that cache, `get()` would run a
query every time it's called, and it's called once per reservation on every
check page (hundreds to thousands of times) for only a handful of distinct
policy texts - a real N+1 query problem that showed up as multi-second page
loads, not a cost inherent to using a database.
"""

from __future__ import annotations

import re

from db import SessionLocal
from models import CancellationRuleRecord

FEE_FIRST_NIGHT = "first_night"
FEE_FULL_STAY = "full_stay"

FEE_LABELS = {FEE_FIRST_NIGHT: "1st Night", FEE_FULL_STAY: "Full Stay"}

_AUTO_PATTERNS = [
    (re.compile(r"^1st\s*N\s*(\d+)\s*D\s*Prior$", re.IGNORECASE), FEE_FIRST_NIGHT),
    (re.compile(r"^NRF\s*Full\s*(\d+)\s*D\s*Prior$", re.IGNORECASE), FEE_FULL_STAY),
    (re.compile(r"^NRF\s*Full$", re.IGNORECASE), FEE_FULL_STAY),  # always_late, no days group
]
_DOA_PATTERN = re.compile(r"^Flexible\s*(\d{1,2})(?::?(\d{2}))?\s*(AM|PM)\s*DOA$", re.IGNORECASE)


def normalize(rule_text: str) -> str:
    return re.sub(r"\s+", " ", rule_text.strip()).lower()


def _auto_detect(rule_text: str) -> dict:
    cleaned = rule_text.strip()

    doa_match = _DOA_PATTERN.match(cleaned)
    if doa_match:
        hour = int(doa_match.group(1))
        minute = int(doa_match.group(2) or 0)
        meridiem = doa_match.group(3).upper()
        if meridiem == "PM" and hour != 12:
            hour += 12
        if meridiem == "AM" and hour == 12:
            hour = 0
        return {
            "display_name": cleaned,
            "fee_type": FEE_FIRST_NIGHT,
            "always_late": False,
            "days_before_arrival": 0,
            "cutoff_time": f"{hour:02d}:{minute:02d}",
            "configured": True,
            "source": "auto",
        }

    for pattern, fee_type in _AUTO_PATTERNS:
        match = pattern.match(cleaned)
        if match:
            groups = match.groups()
            if groups:
                return {
                    "display_name": cleaned,
                    "fee_type": fee_type,
                    "always_late": False,
                    "days_before_arrival": int(groups[0]),
                    "cutoff_time": None,
                    "configured": True,
                    "source": "auto",
                }
            return {
                "display_name": cleaned,
                "fee_type": fee_type,
                "always_late": True,
                "days_before_arrival": None,
                "cutoff_time": None,
                "configured": True,
                "source": "auto",
            }

    return {
        "display_name": cleaned,
        "fee_type": None,
        "always_late": None,
        "days_before_arrival": None,
        "cutoff_time": None,
        "configured": False,
        "source": "auto",
    }


def _row_to_dict(row: CancellationRuleRecord) -> dict:
    return {
        "display_name": row.display_name,
        "fee_type": row.fee_type,
        "always_late": row.always_late,
        "days_before_arrival": row.days_before_arrival,
        "cutoff_time": row.cutoff_time,
        "configured": row.configured,
        "source": row.source,
    }


class RuleStore:
    def __init__(self):
        with SessionLocal() as session:
            self._cache: dict[str, dict] = {
                row.rule_key: _row_to_dict(row) for row in session.query(CancellationRuleRecord).all()
            }

    def ensure_known(self, rule_texts: set[str]) -> None:
        """Make sure every rule text seen in the data has a store entry,
        auto-detecting new ones. Never overwrites an existing entry. A no-op
        DB-wise (just cache lookups) once every text has already been seen
        once, which is the steady-state case on every page load."""
        new_entries = {}
        for text in rule_texts:
            key = normalize(text)
            if key not in self._cache:
                new_entries[key] = _auto_detect(text)
        if not new_entries:
            return
        with SessionLocal() as session:
            for key, entry in new_entries.items():
                session.add(CancellationRuleRecord(rule_key=key, **entry))
            session.commit()
        self._cache.update(new_entries)

    def get(self, rule_text: str) -> dict:
        return self._cache.get(normalize(rule_text)) or _auto_detect(rule_text)

    def all(self) -> dict[str, dict]:
        return dict(self._cache)

    def update(
        self,
        key: str,
        fee_type: str,
        always_late: bool,
        days_before_arrival: int | None,
        cutoff_time: str | None,
    ) -> None:
        with SessionLocal() as session:
            row = session.get(CancellationRuleRecord, key)
            if row is None:
                row = CancellationRuleRecord(rule_key=key, display_name=key)
                session.add(row)
            row.fee_type = fee_type
            row.always_late = always_late
            row.days_before_arrival = None if always_late else days_before_arrival
            row.cutoff_time = None if always_late else cutoff_time
            row.configured = True
            row.source = "manual"
            session.commit()
            self._cache[key] = _row_to_dict(row)
