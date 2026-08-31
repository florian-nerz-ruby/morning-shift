"""One-time import of the existing JSON config files into the database.

Run this once when cutting over from file-based storage to the database:

    python migrate_config_data.py

It reads whatever real configuration is still sitting in data/*.json and
inserts it directly via the ORM models - deliberately NOT through
RuleStore.update()/PropertyStore.update() (those always stamp source as
"manual", which would be wrong here: this needs to preserve whether each
entry was originally auto-detected or manually configured).

Safe to re-run: every insert is a plain "skip if the row already exists",
so running this twice is a no-op the second time.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from db import SessionLocal, init_db
from models import (
    AppSettingRecord,
    BalanceRulePropertyRecord,
    BalanceRuleRecord,
    CancellationRuleRecord,
    PropertyRecord,
)

DATA_DIR = Path(__file__).parent / "data"


def _load_json(name: str) -> dict:
    path = DATA_DIR / name
    if not path.exists():
        print(f"  (skipping {name} - not found)")
        return {}
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def migrate_properties(session) -> None:
    data = _load_json("property_names.json")
    count = 0
    for code, entry in data.items():
        if session.get(PropertyRecord, code) is not None:
            continue
        session.add(
            PropertyRecord(
                code=code,
                name=entry.get("name", code),
                name_configured=entry.get("name_configured", entry.get("configured", False)),
                name_source=entry.get("name_source", entry.get("source", "auto")),
                currency=entry.get("currency", "EUR"),
                currency_configured=entry.get("currency_configured", False),
                currency_source=entry.get("currency_source", "auto"),
            )
        )
        count += 1
    print(f"  properties: {count} imported")


def migrate_cancellation_rules(session) -> None:
    data = _load_json("rule_config.json")
    count = 0
    for key, entry in data.items():
        if session.get(CancellationRuleRecord, key) is not None:
            continue
        session.add(
            CancellationRuleRecord(
                rule_key=key,
                display_name=entry.get("display_name", key),
                fee_type=entry.get("fee_type"),
                always_late=entry.get("always_late"),
                days_before_arrival=entry.get("days_before_arrival"),
                cutoff_time=entry.get("cutoff_time"),
                configured=entry.get("configured", False),
                source=entry.get("source", "auto"),
            )
        )
        count += 1
    print(f"  cancellation_rules: {count} imported")


def migrate_balance_rules(session) -> None:
    data = _load_json("inhouse_balance_rules.json")
    if not data:
        print("  balance_rules: 0 imported")
        return

    if session.get(AppSettingRecord, "balance_default_threshold") is None:
        session.add(AppSettingRecord(key="balance_default_threshold", value=str(data.get("default", -70.0))))

    count = 0
    for rule in data.get("rules", []):
        if session.get(BalanceRuleRecord, rule["id"]) is not None:
            continue
        session.add(BalanceRuleRecord(id=rule["id"], label=rule["label"], threshold=rule["threshold"]))
        for code in rule.get("properties", []):
            session.add(BalanceRulePropertyRecord(rule_id=rule["id"], property_code=code))
        count += 1
    print(f"  balance_rules: {count} imported")


def migrate_rooms_threshold(session) -> None:
    data = _load_json("inhouse_rooms_threshold.json")
    if not data:
        print("  rooms_threshold: not set (default will apply)")
        return
    if session.get(AppSettingRecord, "rooms_threshold") is None:
        session.add(AppSettingRecord(key="rooms_threshold", value=str(data.get("threshold", 1.0))))
        print(f"  rooms_threshold: imported ({data.get('threshold')})")
    else:
        print("  rooms_threshold: already set, left alone")


def main() -> None:
    init_db()
    with SessionLocal() as session:
        print("Migrating config data from data/*.json into the database...")
        migrate_properties(session)
        migrate_cancellation_rules(session)
        migrate_balance_rules(session)
        migrate_rooms_threshold(session)
        # Legacy handled state contains raw property/reservation identifiers.
        # It cannot be safely migrated without resolving it against decrypted
        # PMS records, so the encrypted cutover intentionally starts fresh.
        print("  handled state: skipped (legacy raw identifiers are not imported)")
        session.commit()
    print("Done.")


if __name__ == "__main__":
    main()
