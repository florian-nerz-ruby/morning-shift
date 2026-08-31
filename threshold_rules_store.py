"""Default + named property-group rules engine for the in-house balance
threshold.

One store holds:
  - a single default threshold that applies to any property not covered by
    a rule below (persisted as a row in `app_settings`)
  - a short list of named rules (`balance_rules`), each with its own
    threshold and the set of properties it applies to
    (`balance_rule_properties`)

A property belongs to at most one rule at a time - `property_code` carries a
UNIQUE constraint in `balance_rule_properties` (see models.py), so the
database itself refuses to let a property end up in two rules, not just
ThresholdRuleStore's own bookkeeping. Saving a rule with a given property
removes that property from whichever other rule had it first, so there's
never an ambiguous "which rule wins" to resolve.

Reads (get_threshold, rules, default_threshold) are served from an
in-memory cache loaded once at startup, since get_threshold() is called once
per in-house reservation on every page load (up to a couple thousand times) -
without the cache that's a couple thousand queries for what's really only a
handful of distinct rules, a real N+1 problem rather than a cost inherent to
using a database. Writes are rare admin actions (someone editing a rule in
Settings), so they just reload the cache fully afterwards rather than
patching it incrementally - simplest-correct given how infrequently they run.

This is currently only used for the balance threshold - the room-count
threshold turned out not to need per-property variation at all, so it's a
single flat setting in app.py instead. If a second metric ever does need
this same rule-engine treatment, the table names below would need a
`metric` column to keep them apart; not worth building ahead of that need.
"""

from __future__ import annotations

import secrets

from db import SessionLocal
from models import AppSettingRecord, BalanceRuleRecord, BalanceRulePropertyRecord

_DEFAULT_SETTING_KEY = "balance_default_threshold"


class ThresholdRuleStore:
    def __init__(self, default_threshold: float):
        self._fallback_default = default_threshold
        self._load_cache()

    def _load_cache(self) -> None:
        with SessionLocal() as session:
            setting = session.get(AppSettingRecord, _DEFAULT_SETTING_KEY)
            self._default = float(setting.value) if setting else self._fallback_default

            rules: dict[str, dict] = {
                r.id: {"id": r.id, "label": r.label, "threshold": r.threshold, "properties": []}
                for r in session.query(BalanceRuleRecord).all()
            }
            property_index: dict[str, str] = {}
            for link in session.query(BalanceRulePropertyRecord).all():
                rules[link.rule_id]["properties"].append(link.property_code)
                property_index[link.property_code] = link.rule_id

        self._rules = rules
        self._property_index = property_index

    @property
    def default_threshold(self) -> float:
        return self._default

    def set_default(self, threshold: float) -> None:
        with SessionLocal() as session:
            row = session.get(AppSettingRecord, _DEFAULT_SETTING_KEY)
            if row is None:
                session.add(AppSettingRecord(key=_DEFAULT_SETTING_KEY, value=str(threshold)))
            else:
                row.value = str(threshold)
            session.commit()
        self._default = threshold

    def rules(self) -> list[dict]:
        return [{**r, "properties": list(r["properties"])} for r in self._rules.values()]

    def get_threshold(self, property_code: str) -> tuple[float, str | None]:
        """Returns (threshold, rule_label) - rule_label is None when the
        property falls through to the default."""
        rule_id = self._property_index.get(property_code)
        if rule_id is not None:
            rule = self._rules[rule_id]
            return rule["threshold"], rule["label"]
        return self._default, None

    def save_rule(self, rule_id: str, label: str, threshold: float, properties: list[str]) -> None:
        with SessionLocal() as session:
            # Mutual exclusivity: pull these properties out of any other rule first.
            if properties:
                session.query(BalanceRulePropertyRecord).filter(
                    BalanceRulePropertyRecord.property_code.in_(properties),
                    BalanceRulePropertyRecord.rule_id != rule_id,
                ).delete(synchronize_session=False)

            rule = session.get(BalanceRuleRecord, rule_id)
            if rule is None:
                session.add(BalanceRuleRecord(id=rule_id, label=label, threshold=threshold))
            else:
                rule.label = label
                rule.threshold = threshold

            # Replace this rule's property set wholesale - simplest correct
            # approach at this scale (a handful of rules, a couple dozen
            # properties at most), no need for a diff.
            session.query(BalanceRulePropertyRecord).filter_by(rule_id=rule_id).delete(
                synchronize_session=False
            )
            for code in properties:
                session.add(BalanceRulePropertyRecord(rule_id=rule_id, property_code=code))

            session.commit()
        self._load_cache()

    def add_blank_rule(self) -> str:
        rule_id = secrets.token_hex(6)
        with SessionLocal() as session:
            session.add(BalanceRuleRecord(id=rule_id, label="New rule", threshold=self._default))
            session.commit()
        self._load_cache()
        return rule_id

    def delete_rule(self, rule_id: str) -> None:
        with SessionLocal() as session:
            session.query(BalanceRulePropertyRecord).filter_by(rule_id=rule_id).delete(
                synchronize_session=False
            )
            session.query(BalanceRuleRecord).filter_by(id=rule_id).delete(synchronize_session=False)
            session.commit()
        self._load_cache()
