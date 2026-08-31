"""Shared dedup logic for the raw PMS exports.

Both the cancellation and in-house exports have one row per (reservation,
external reference type), so a reservation with a CRS link, a Shiji link,
and a travel-agent locator shows up as three raw rows. These functions
collapse that down to one row per reservation, keyed by
(PROPERTY, RESERVATION_ID), with the SHIJI/TA_RECORD_LOCATOR rows folded
into is_shiji/shiji_number/ta_locator fields on that one row.

This is deliberately independent of Alfred's web app and of CSV specifically
- it operates on any iterable of dicts keyed by the PMS column names, so the
same logic can be reused by an external ingestion pipeline (reading from a
CSV, an API, wherever) that populates the `cancellations` /
`inhouse_reservations` tables directly. Alfred itself only uses this for
local seeding/testing (see seed_from_csv.py) - in production the tables are
expected to already be deduplicated by whatever wrote them.

Each function returns (rows, property_facts):
  rows            one dict per reservation, ready to upsert into the
                  corresponding database table
  property_facts  {property_code: {"name": str, "currency": str}} for
                  every PROPERTY_NAME / CURRENCY value actually seen (empty
                  string if that column was blank or absent) - feed this into
                  PropertyStore.ensure_known_names() / ensure_known_currencies()
"""

from __future__ import annotations

from datetime import datetime
from typing import Iterable


def _first_seen(seen: dict[str, str], property_code: str, value: str) -> None:
    value = (value or "").strip()
    if value and not seen.get(property_code):
        seen[property_code] = value
    else:
        seen.setdefault(property_code, "")


def _property_facts(names: dict[str, str], currencies: dict[str, str]) -> dict[str, dict]:
    codes = set(names) | set(currencies)
    return {code: {"name": names.get(code, ""), "currency": currencies.get(code, "")} for code in codes}


def dedupe_cancellation_rows(raw_rows: Iterable[dict]) -> tuple[list[dict], dict[str, dict]]:
    """raw_rows: dicts with PROPERTY, CONFIRMATION_NUMBER, RESERVATION_ID,
    RESERVATION_STATUS, CANCEL_DATE, CANCEL_TIME, ARRIVAL_DATE,
    CANCELLATION_RULE, RATE_CODE, NIGHTS, SOURCE_CODE_DESCRIPTION,
    EXTERNAL_REF_TYPE, EXTERNAL_REF_NUMBER, and optionally PROPERTY_NAME /
    CURRENCY (either can be missing entirely or blank per row)."""
    grouped: dict[tuple[str, str], dict] = {}
    order: list[tuple[str, str]] = []
    names_seen: dict[str, str] = {}
    currencies_seen: dict[str, str] = {}

    for raw in raw_rows:
        if not raw.get("CONFIRMATION_NUMBER") or not raw.get("RESERVATION_ID"):
            continue

        property_code = raw["PROPERTY"].strip()
        reservation_id = raw["RESERVATION_ID"].strip()
        group_key = (property_code, reservation_id)

        _first_seen(names_seen, property_code, raw.get("PROPERTY_NAME", ""))
        _first_seen(currencies_seen, property_code, raw.get("CURRENCY", ""))

        if group_key not in grouped:
            order.append(group_key)
            grouped[group_key] = {
                "property_code": property_code,
                "confirmation_number": raw["CONFIRMATION_NUMBER"].strip(),
                "reservation_id": reservation_id,
                "status": raw["RESERVATION_STATUS"].strip(),
                "cancel_date": datetime.strptime(raw["CANCEL_DATE"].strip(), "%Y-%m-%d").date(),
                "cancel_time": datetime.strptime(raw["CANCEL_TIME"].strip(), "%H:%M").time(),
                "arrival_date": datetime.strptime(raw["ARRIVAL_DATE"].strip(), "%Y-%m-%d").date(),
                "rule_text": raw["CANCELLATION_RULE"].strip(),
                "rate_code": (raw.get("RATE_CODE") or "").strip(),
                "nights": int(raw["NIGHTS"]) if raw.get("NIGHTS") else 0,
                "source_description": (raw.get("SOURCE_CODE_DESCRIPTION") or "").strip(),
                "is_shiji": False,
                "shiji_number": None,
                "ta_locator": None,
            }

        ref_type = raw.get("EXTERNAL_REF_TYPE", "").strip().upper()
        if ref_type == "SHIJI":
            grouped[group_key]["is_shiji"] = True
            grouped[group_key]["shiji_number"] = (raw.get("EXTERNAL_REF_NUMBER") or "").strip() or None
        elif ref_type == "TA_RECORD_LOCATOR":
            grouped[group_key]["ta_locator"] = (raw.get("EXTERNAL_REF_NUMBER") or "").strip() or None

    rows = [grouped[key] for key in order]
    return rows, _property_facts(names_seen, currencies_seen)


def dedupe_inhouse_rows(raw_rows: Iterable[dict]) -> tuple[list[dict], dict[str, dict]]:
    """raw_rows: dicts with PROPERTY, CONFIRMATION_NUMBER, RESERVATION_ID,
    RESERVATION_STATUS, RATE_CODE, ARRIVAL_DATE, DEPARTURE_DATE, BALANCE,
    NUMBER_OF_ROOMS (optional), SOURCE_CODE_DESCRIPTION, EXTERNAL_REF_TYPE,
    EXTERNAL_REF_NUMBER, and optionally PROPERTY_NAME / CURRENCY."""
    grouped: dict[tuple[str, str], dict] = {}
    order: list[tuple[str, str]] = []
    names_seen: dict[str, str] = {}
    currencies_seen: dict[str, str] = {}

    for raw in raw_rows:
        if not raw.get("CONFIRMATION_NUMBER") or not raw.get("RESERVATION_ID"):
            continue

        property_code = raw["PROPERTY"].strip()
        reservation_id = raw["RESERVATION_ID"].strip()
        group_key = (property_code, reservation_id)

        _first_seen(names_seen, property_code, raw.get("PROPERTY_NAME", ""))
        _first_seen(currencies_seen, property_code, raw.get("CURRENCY", ""))

        if group_key not in grouped:
            order.append(group_key)
            grouped[group_key] = {
                "property_code": property_code,
                "confirmation_number": raw["CONFIRMATION_NUMBER"].strip(),
                "reservation_id": reservation_id,
                "status": raw["RESERVATION_STATUS"].strip(),
                "rate_code": (raw.get("RATE_CODE") or "").strip(),
                "arrival_date": datetime.strptime(raw["ARRIVAL_DATE"].strip(), "%Y-%m-%d").date(),
                "departure_date": datetime.strptime(raw["DEPARTURE_DATE"].strip(), "%Y-%m-%d").date(),
                "balance": _parse_float(raw.get("BALANCE", "")),
                "number_of_rooms": int(raw["NUMBER_OF_ROOMS"]) if raw.get("NUMBER_OF_ROOMS") else 1,
                "source_description": (raw.get("SOURCE_CODE_DESCRIPTION") or "").strip(),
                "is_shiji": False,
                "shiji_number": None,
                "ta_locator": None,
            }

        ref_type = raw.get("EXTERNAL_REF_TYPE", "").strip().upper()
        if ref_type == "SHIJI":
            grouped[group_key]["is_shiji"] = True
            grouped[group_key]["shiji_number"] = (raw.get("EXTERNAL_REF_NUMBER") or "").strip() or None
        elif ref_type == "TA_RECORD_LOCATOR":
            grouped[group_key]["ta_locator"] = (raw.get("EXTERNAL_REF_NUMBER") or "").strip() or None

    rows = [grouped[key] for key in order]
    return rows, _property_facts(names_seen, currencies_seen)


def _parse_float(value: str) -> float:
    cleaned = (value or "").strip().replace(",", "")
    return float(cleaned) if cleaned else 0.0


def first_non_empty_by_property(rows: list[dict], code_field: str, value_field: str) -> dict[str, str]:
    """{property_code: first non-empty value_field seen across rows}.

    Used when reading already-deduplicated reservation rows back out of the
    database, to feed PropertyStore.ensure_known_names() /
    ensure_known_currencies() - a property's name/currency may be blank on
    some rows and populated on others, so this picks the first real value
    rather than whichever row happens to be read last.
    """
    seen: dict[str, str] = {}
    for row in rows:
        code = row[code_field]
        value = (row.get(value_field) or "").strip()
        if value and not seen.get(code):
            seen[code] = value
        else:
            seen.setdefault(code, "")
    return seen
