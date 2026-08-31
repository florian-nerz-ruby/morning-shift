"""Alfred - a lightweight morning-shift task assistant for the reservations team.

Reads currently-known cancellations and in-house reservations from the
database (populated by an external ingestion pipeline - see dedup.py) and
flags the ones that need attention: cancellations made too late relative to
their policy, and in-house reservations with an odd balance or room count.
"""

from __future__ import annotations

import hashlib
import os
import re
import secrets
from datetime import datetime, timedelta
from pathlib import Path

from flask import Flask, jsonify, redirect, render_template, request, session, url_for

from cancellation_logic import load_cancellations
from db import SessionLocal, init_db
from inhouse_logic import load_inhouse_reservations
from models import AppSettingRecord, HandledFindingRecord
from property_store import PropertyStore
from rules_store import FEE_LABELS, RuleStore
from threshold_rules_store import ThresholdRuleStore

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
SECRET_KEY_PATH = DATA_DIR / ".flask_secret_key"
DEFAULT_ROOMS_THRESHOLD = 1.0
VALID_CHECK_TYPES = {"cancellation", "inhouse"}
ROOMS_THRESHOLD_SETTING_KEY = "rooms_threshold"
HMAC_LOOKUP_RE = re.compile(r"^[a-f0-9]{64}$")


def get_or_create_secret_key() -> str:
    # Deliberately still a local file/env concern, not app data - a
    # deployment secret, not something that belongs in the database.
    configured_secret = os.environ.get("FLASK_SECRET_KEY", "").strip()
    if configured_secret:
        if len(configured_secret) < 32:
            raise RuntimeError("FLASK_SECRET_KEY must contain at least 32 characters")
        return configured_secret
    if SECRET_KEY_PATH.exists():
        return SECRET_KEY_PATH.read_text(encoding="utf-8").strip()
    DATA_DIR.mkdir(exist_ok=True)
    key = secrets.token_hex(32)
    SECRET_KEY_PATH.write_text(key, encoding="utf-8")
    return key


app = Flask(__name__)
app.secret_key = get_or_create_secret_key()
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(hours=16)
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = os.environ.get("FLASK_SESSION_COOKIE_SECURE", "").lower() == "true"

init_db()
rule_store = RuleStore()
property_store = PropertyStore()
balance_rules = ThresholdRuleStore(default_threshold=-70.0)
THRESHOLD_STORES = {"balance": balance_rules}


def get_rooms_threshold() -> float:
    # A single flag-out-if-more-than-this number, same for every property -
    # unlike the balance buffer, room count isn't a currency or per-property
    # concern, so it doesn't need the full rule-engine treatment.
    with SessionLocal() as db_session:
        row = db_session.get(AppSettingRecord, ROOMS_THRESHOLD_SETTING_KEY)
        return float(row.value) if row else DEFAULT_ROOMS_THRESHOLD


def set_rooms_threshold(threshold: float) -> None:
    with SessionLocal() as db_session:
        row = db_session.get(AppSettingRecord, ROOMS_THRESHOLD_SETTING_KEY)
        if row is None:
            db_session.add(AppSettingRecord(key=ROOMS_THRESHOLD_SETTING_KEY, value=str(threshold)))
        else:
            row.value = str(threshold)
        db_session.commit()


def load_handled_map(check: str) -> dict[str, tuple[str, str]]:
    """{opaque entity lookup: (fingerprint, handled_at)} for one check type."""
    with SessionLocal() as db_session:
        rows = db_session.query(HandledFindingRecord).filter_by(check_type=check).all()
        return {
            r.entity_lookup: (r.finding_fingerprint, r.handled_at.isoformat(timespec="seconds"))
            for r in rows
        }


def set_handled(check: str, key: str, fingerprint: str, handled: bool) -> None:
    with SessionLocal() as db_session:
        row = db_session.get(HandledFindingRecord, (check, key))
        if handled:
            if row is None:
                db_session.add(
                    HandledFindingRecord(
                        check_type=check, entity_lookup=key, finding_fingerprint=fingerprint,
                        handled_at=datetime.utcnow(),
                    )
                )
            else:
                row.finding_fingerprint = fingerprint
                row.handled_at = datetime.utcnow()
        elif row is not None:
            db_session.delete(row)
        db_session.commit()


def get_selected_properties(known_properties: set[str]) -> set[str]:
    """Per-browser-session hotel scope. Defaults to everything until the
    person using this browser narrows it down themselves."""
    if "selected_properties" not in session:
        return set(known_properties)
    return set(session["selected_properties"])


def hotels_confirmed() -> bool:
    return bool(session.get("hotels_confirmed"))


def set_selected_properties(selected: set[str]) -> None:
    session.permanent = True
    session["selected_properties"] = sorted(selected)
    session["hotels_confirmed"] = True


def get_all_cancellations():
    cancellations = load_cancellations(rule_store, property_store)
    handled_map = load_handled_map("cancellation")
    for c in cancellations:
        handled = handled_map.get(c.key)
        c.handled = handled is not None and handled[0] == c.finding_fingerprint
        c.handled_at = handled[1] if c.handled and handled is not None else None
    return cancellations


def get_all_inhouse_reservations():
    reservations = load_inhouse_reservations(property_store, balance_rules, get_rooms_threshold())
    handled_map = load_handled_map("inhouse")
    for r in reservations:
        handled = handled_map.get(r.key)
        r.handled = handled is not None and handled[0] == r.finding_fingerprint
        r.handled_at = handled[1] if r.handled and handled is not None else None
    return reservations


def get_property_directory(*record_lists) -> list[tuple[str, str]]:
    """(code, hotel_name) pairs across any number of record lists (each check
    can introduce properties the others don't have), sorted by name."""
    names = {}
    for records in record_lists:
        for r in records:
            names[r.property_code] = r.hotel_name
    return sorted(names.items(), key=lambda pair: pair[1].lower())


@app.route("/")
def home():
    all_cancellations = get_all_cancellations()
    all_inhouse = get_all_inhouse_reservations()
    property_directory = get_property_directory(all_cancellations, all_inhouse)
    known_properties = {code for code, _ in property_directory}
    selected = get_selected_properties(known_properties)

    scoped_cancellations = [c for c in all_cancellations if c.property_code in selected]
    late = [c for c in scoped_cancellations if c.is_late]
    late_open = [c for c in late if not c.handled]
    report_date = max((c.cancel_date for c in all_cancellations), default=None)

    scoped_inhouse = [r for r in all_inhouse if r.property_code in selected]
    flagged = [r for r in scoped_inhouse if r.is_flagged]
    flagged_open = [r for r in flagged if not r.handled]

    return render_template(
        "home.html",
        report_date=report_date,
        has_data=bool(all_cancellations),
        late_count=len(late),
        late_open_count=len(late_open),
        has_inhouse_data=bool(all_inhouse),
        flagged_open_count=len(flagged_open),
        property_directory=property_directory,
        selected_properties=selected,
        hotels_confirmed=hotels_confirmed(),
    )


@app.route("/api/properties", methods=["POST"])
def save_properties():
    selected = set(request.form.getlist("property"))
    set_selected_properties(selected)
    return redirect(url_for("home"))


@app.route("/cancellation-check")
def cancellation_check():
    all_cancellations = get_all_cancellations()
    property_directory = get_property_directory(all_cancellations)
    known_properties = {code for code, _ in property_directory}
    selected = get_selected_properties(known_properties)
    scoped = [c for c in all_cancellations if c.property_code in selected]

    report_date = max((c.cancel_date for c in all_cancellations), default=None)

    # Late reservations that still need attention first, grouped by hotel
    # (so an agent can clear one property at a time), then by arrival date.
    # Sorted on the real date objects, before formatting them for display.
    scoped.sort(key=lambda c: (c.handled, not c.is_late, c.hotel_name.lower(), c.arrival_date))

    rows = [
        {
            "key": c.key,
            "finding_fingerprint": c.finding_fingerprint,
            "property": c.property_code,
            "hotel_name": c.hotel_name,
            "confirmation_number": c.confirmation_number,
            "cancel_date": c.cancel_date.strftime("%d %b %y"),
            "cancel_time": c.cancel_time.strftime("%H:%M"),
            "arrival_date": c.arrival_date.strftime("%d %b %y"),
            "nights": c.nights,
            "rate_code": c.rate_code,
            "source_description": c.source_description,
            "is_shiji": c.is_shiji,
            "shiji_number": c.shiji_number,
            "ta_locator": c.ta_locator,
            "rule_text": c.rule_text,
            "deadline_text": c.deadline_text,
            "days_prior": c.days_prior,
            "is_late": c.is_late,
            "configured": c.configured,
            "fee_type": FEE_LABELS.get(c.fee_type, c.fee_type),
            "reason": c.reason,
            "handled": c.handled,
            "opera_url": c.opera_url,
        }
        for c in scoped
    ]

    return render_template(
        "cancellation_check.html",
        report_date=report_date,
        properties=[(code, name) for code, name in property_directory if code in selected],
        rows=rows,
        late_count=sum(1 for r in rows if r["is_late"] and not r["handled"]),
    )


@app.route("/in-house-check")
def inhouse_check():
    all_inhouse = get_all_inhouse_reservations()
    property_directory = get_property_directory(all_inhouse)
    known_properties = {code for code, _ in property_directory}
    selected = get_selected_properties(known_properties)
    scoped = [r for r in all_inhouse if r.property_code in selected]

    # Flagged reservations that still need attention first, grouped by hotel,
    # then by arrival date. Sorted on the real date objects, before
    # formatting them for display.
    scoped.sort(key=lambda r: (r.handled, not r.is_flagged, r.hotel_name.lower(), r.arrival_date))

    rows = [
        {
            "key": r.key,
            "finding_fingerprint": r.finding_fingerprint,
            "property": r.property_code,
            "hotel_name": r.hotel_name,
            "confirmation_number": r.confirmation_number,
            "arrival_date": r.arrival_date.strftime("%d %b %y"),
            "departure_date": r.departure_date.strftime("%d %b %y"),
            "nights": r.nights,
            "rate_code": r.rate_code,
            "source_description": r.source_description,
            "is_shiji": r.is_shiji,
            "shiji_number": r.shiji_number,
            "ta_locator": r.ta_locator,
            "balance_display": r.balance_display,
            "number_of_rooms": r.number_of_rooms,
            "is_flagged": r.is_flagged,
            "flag_reasons": r.flag_reasons,
            "severity": r.severity,
            "handled": r.handled,
            "opera_url": r.opera_url,
        }
        for r in scoped
    ]

    return render_template(
        "inhouse_check.html",
        properties=[(code, name) for code, name in property_directory if code in selected],
        rows=rows,
        flagged_count=sum(1 for r in rows if r["is_flagged"] and not r["handled"]),
    )


@app.route("/api/handled", methods=["POST"])
def toggle_handled():
    payload = request.get_json(force=True)
    key = payload.get("key")
    fingerprint = payload.get("finding_fingerprint")
    handled = bool(payload.get("handled"))
    check = payload.get("check")
    if (
        not isinstance(key, str)
        or not isinstance(fingerprint, str)
        or not HMAC_LOOKUP_RE.fullmatch(key)
        or not HMAC_LOOKUP_RE.fullmatch(fingerprint)
        or check not in VALID_CHECK_TYPES
    ):
        return jsonify({"error": "missing or invalid key/check"}), 400

    set_handled(check, key, fingerprint, handled)
    return jsonify({"ok": True})


def build_threshold_engine_context(store: ThresholdRuleStore, known_properties, currency_sensitive: bool) -> dict:
    """Everything the Settings page needs to display and edit one
    ThresholdRuleStore: its rules (with member names for display), and the
    resolved effective threshold for every known property."""
    rules_display = []
    for rule in store.rules():
        rules_display.append({**rule, "member_names": [property_store.get(c) for c in rule["properties"]]})

    resolved = []
    for code, name in known_properties:
        threshold, rule_label = store.get_threshold(code)
        currency = property_store.get_currency(code)
        # Flagged when nobody has ever looked at this property for this
        # rule (still riding the untouched default) and its currency isn't
        # the one the default was chosen for - not relevant for a
        # currency-independent metric like room count.
        needs_review = currency_sensitive and rule_label is None and currency != "EUR"
        resolved.append(
            {
                "code": code,
                "name": name,
                "currency": currency,
                "threshold": threshold,
                "source": rule_label or "Default",
                "needs_review": needs_review,
            }
        )
    resolved.sort(key=lambda r: (not r["needs_review"], r["name"].lower()))

    return {
        "default": store.default_threshold,
        "rules": rules_display,
        "resolved": resolved,
    }


@app.route("/settings")
def settings():
    # Ensures both stores have discovered everything currently known, across
    # both checks - a property code that only shows up in one export still
    # gets a row on the Hotel Names table.
    all_cancellations = get_all_cancellations()
    all_inhouse = get_all_inhouse_reservations()
    known_properties = get_property_directory(all_cancellations, all_inhouse)

    rules = []
    for key, rule in rule_store.all().items():
        # A stable, key-derived id (not the row's render position, which
        # shifts whenever a rule's configured-status changes) so a browser
        # restoring cached field values on back/reload can't attach them
        # to the wrong rule.
        slug = hashlib.md5(key.encode("utf-8")).hexdigest()[:10]
        rules.append({"key": key, "slug": slug, **rule})
    rules.sort(key=lambda r: (r["configured"], r["display_name"].lower()))

    properties = [{"code": code, **entry} for code, entry in property_store.all().items()]
    properties.sort(key=lambda p: (p["name_configured"], p["code"]))

    property_currencies = {code: property_store.get_currency(code) for code, _ in known_properties}
    distinct_currencies = sorted(set(property_currencies.values()))

    return render_template(
        "settings.html",
        rules=rules,
        fee_labels=FEE_LABELS,
        properties=properties,
        known_properties=known_properties,
        property_currencies=property_currencies,
        distinct_currencies=distinct_currencies,
        balance_data=build_threshold_engine_context(balance_rules, known_properties, currency_sensitive=True),
        rooms_threshold=get_rooms_threshold(),
    )


@app.route("/settings/rules", methods=["POST"])
def update_rule():
    rule_key = request.form.get("rule_key", "")
    fee_type = request.form.get("fee_type")
    deadline_type = request.form.get("deadline_type")
    always_late = deadline_type == "non_ref"
    days_raw = request.form.get("days_before_arrival", "").strip()
    days_before_arrival = int(days_raw) if days_raw else 0
    cutoff_time = request.form.get("cutoff_time", "").strip() or None

    rule_store.update(
        key=rule_key,
        fee_type=fee_type,
        always_late=always_late,
        days_before_arrival=days_before_arrival,
        cutoff_time=cutoff_time,
    )
    return redirect(url_for("settings"))


@app.route("/settings/properties", methods=["POST"])
def update_property():
    code = request.form.get("code", "")
    name = request.form.get("name", "")
    currency = request.form.get("currency", "")
    property_store.update(code, name=name, currency=currency)
    return redirect(url_for("settings"))


def _parse_threshold(raw: str, fallback: float) -> float:
    try:
        return float(raw.strip())
    except (ValueError, AttributeError):
        return fallback


@app.route("/settings/rules/<metric>/default", methods=["POST"])
def update_rule_default(metric: str):
    store = THRESHOLD_STORES.get(metric)
    if not store:
        return jsonify({"error": "unknown metric"}), 400
    store.set_default(_parse_threshold(request.form.get("threshold", ""), store.default_threshold))
    return redirect(url_for("settings"))


@app.route("/settings/rules/<metric>/save", methods=["POST"])
def save_threshold_rule(metric: str):
    store = THRESHOLD_STORES.get(metric)
    if not store:
        return jsonify({"error": "unknown metric"}), 400
    rule_id = request.form.get("rule_id", "")
    label = request.form.get("label", "").strip() or "Unnamed rule"
    threshold = _parse_threshold(request.form.get("threshold", ""), store.default_threshold)
    properties = request.form.getlist("properties")
    store.save_rule(rule_id, label, threshold, properties)
    return redirect(url_for("settings"))


@app.route("/settings/rules/<metric>/add", methods=["POST"])
def add_threshold_rule(metric: str):
    store = THRESHOLD_STORES.get(metric)
    if not store:
        return jsonify({"error": "unknown metric"}), 400
    store.add_blank_rule()
    return redirect(url_for("settings"))


@app.route("/settings/rules/<metric>/delete", methods=["POST"])
def delete_threshold_rule(metric: str):
    store = THRESHOLD_STORES.get(metric)
    if not store:
        return jsonify({"error": "unknown metric"}), 400
    store.delete_rule(request.form.get("rule_id", ""))
    return redirect(url_for("settings"))


@app.route("/settings/rooms-threshold", methods=["POST"])
def update_rooms_threshold():
    set_rooms_threshold(_parse_threshold(request.form.get("threshold", ""), get_rooms_threshold()))
    return redirect(url_for("settings"))


if __name__ == "__main__":
    app.run(debug=True)
