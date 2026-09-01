"""Business logic for the in-house check task.

Reads currently-known in-house reservations from the `inhouse_reservations`
table and flags reservations whose folio balance looks off, or that have
more than one room on them, so staff know which ones need a closer look
before/during the guest's stay.

The table is populated by an external ingestion pipeline, not by Alfred -
see dedup.py for the shared dedup logic it's expected to use before writing
rows here (one row per reservation, already collapsed from the PMS export's
one-row-per-external-reference-type shape).

Balance rules (a folio's BALANCE is negative while the guest is in credit -
e.g. a deposit or prepayment held against the stay - and positive once they
owe money). These are a severity ladder, not independent checks - a
reservation shows whichever single one is most specific, since a €0 or
positive balance always also technically clears the "buffer too thin"
bar and repeating that fact doesn't help anyone decide what to do:
  1. balance > 0                    -> worst: guest currently owes money
  2. balance == 0                    -> no cushion left at all
  3. balance > balance_threshold     -> credit cushion has worn thin
  (else: balance comfortably negative, no balance flag)
balance_threshold comes from a ThresholdRuleStore (see threshold_rules_store.py)
- a default plus optional per-property-group overrides, since balances
aren't comparable across a portfolio spanning several currencies.

Independently of all that:
  number_of_rooms > rooms_threshold  -> flagged: worth confirming
rooms_threshold is a single flat number, the same for every property - what
counts as "a lot of rooms" isn't a currency or per-property concern the way
balance is, so it doesn't get the rule-engine treatment.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from dedup import first_non_empty_by_property
from pms import load_current_inhouse
from property_store import PropertyStore
from threshold_rules_store import ThresholdRuleStore

OPERA_BOOKMARK_URL = (
    "https://ihgce1.oraclehospitality.eu-frankfurt-1.ocs.oraclecloud.com/"
    "IHGENT/operacloud/bookmarks/reservation?resvId={resv_id}&TPRESORT={property_code}"
)
_ZERO_DECIMAL_CURRENCIES = frozenset({"BIF", "CLP", "DJF", "GNF", "ISK", "JPY", "KMF", "KRW", "MGA", "PYG", "RWF", "UGX", "VND", "VUV", "XAF", "XOF", "XPF"})
_THREE_DECIMAL_CURRENCIES = frozenset({"BHD", "IQD", "JOD", "KWD", "LYD", "OMR", "TND"})


def format_balance(balance: Decimal, currency: str) -> str:
    return f"{balance:,.2f} {currency}"


@dataclass
class InHouseReservation:
    property_code: str
    hotel_name: str
    currency: str
    confirmation_number: str
    reservation_id: str
    status: str
    rate_code: str
    arrival_date: date
    departure_date: date
    balance: Decimal
    number_of_rooms: int
    source_description: str
    is_shiji: bool
    shiji_number: str | None
    ta_locator: str | None
    entity_lookup: str
    finding_fingerprint: str

    is_flagged: bool = field(init=False, default=False)
    flag_reasons: list[dict] = field(init=False, default_factory=list)
    severity: str | None = field(init=False, default=None)
    handled: bool = field(init=False, default=False)
    handled_at: str | None = field(init=False, default=None)

    _SEVERITY_RANK = {"danger": 2, "warning": 1, "info": 0}

    def evaluate(self, balance_threshold: float, rooms_threshold: float) -> None:
        reasons = []
        # A zero folio balance is the expected state on the day the guest
        # departs.  Do not create a balance or room-count task for it.
        if self.departure_date == date.today() and self.balance == 0:
            self.flag_reasons = reasons
            self.is_flagged = False
            self.severity = None
            return

        threshold = Decimal(str(balance_threshold))
        if self.balance > 0:
            reasons.append({"label": "Positive balance", "severity": "danger"})
        elif self.balance == 0:
            reasons.append({"label": "Zero balance", "severity": "warning"})
        elif self.balance > threshold:
            reasons.append({"label": "Low balance buffer", "severity": "warning"})

        if self.number_of_rooms > rooms_threshold:
            reasons.append({"label": f"{self.number_of_rooms} rooms", "severity": "info"})

        self.flag_reasons = reasons
        self.is_flagged = bool(reasons)
        # The worst reason present drives the row's overall visual weight -
        # a reservation flagged only for its room count shouldn't look as
        # alarming as one where the guest actually owes money.
        self.severity = (
            max(reasons, key=lambda r: self._SEVERITY_RANK[r["severity"]])["severity"] if reasons else None
        )

    @property
    def nights(self) -> int:
        return (self.departure_date - self.arrival_date).days

    @property
    def balance_display(self) -> str:
        return format_balance(self.balance, self.currency)

    @property
    def key(self) -> str:
        return self.entity_lookup

    @property
    def opera_url(self) -> str:
        return OPERA_BOOKMARK_URL.format(resv_id=self.reservation_id, property_code=self.property_code)


def load_inhouse_reservations(
    property_store: PropertyStore,
    balance_rules: ThresholdRuleStore,
    rooms_threshold: float,
) -> list[InHouseReservation]:
    records = []
    for payload, entity_lookup, fingerprint in load_current_inhouse():
        balance = payload.get("balance")
        if not isinstance(balance, dict):
            continue
        currency = balance.get("currency")
        amount_minor = balance.get("amount_minor")
        if not isinstance(currency, str) or isinstance(amount_minor, bool) or not isinstance(amount_minor, int):
            continue
        exponent = 0 if currency in _ZERO_DECIMAL_CURRENCIES else 3 if currency in _THREE_DECIMAL_CURRENCIES else 2
        value = Decimal(amount_minor).scaleb(-exponent)
        references = payload.get("external_references", [])
        references = references if isinstance(references, list) else []
        ref_map = {
            str(reference.get("type", "")).upper(): str(reference.get("number", ""))
            for reference in references
            if isinstance(reference, dict) and reference.get("number")
        }
        try:
            records.append(
                {
                    "property_code": str(payload["property_code"]),
                    "confirmation_number": str(payload["confirmation_number"]),
                    "reservation_id": str(payload["reservation_id"]),
                    "status": str(payload["status"]),
                    "rate_code": str(payload.get("rate_code", "")),
                    "arrival_date": date.fromisoformat(str(payload["arrival_date"])),
                    "departure_date": date.fromisoformat(str(payload["departure_date"])),
                    "balance": value,
                    "number_of_rooms": int(payload["number_of_rooms"]),
                    "source_description": str(payload.get("source_description", "")),
                    "is_shiji": "SHIJI" in ref_map,
                    "shiji_number": ref_map.get("SHIJI") or None,
                    "ta_locator": ref_map.get("TA_RECORD_LOCATOR") or None,
                    "property_name": str(payload.get("property_name", "")),
                    "currency": currency,
                    "entity_lookup": entity_lookup,
                    "finding_fingerprint": fingerprint,
                }
            )
        except (KeyError, TypeError, ValueError):
            continue

    property_store.ensure_known_names(first_non_empty_by_property(records, "property_code", "property_name"))
    property_store.ensure_known_currencies(first_non_empty_by_property(records, "property_code", "currency"))

    reservations = []
    for r in records:
        property_code = r["property_code"]
        fields = {k: v for k, v in r.items() if k not in ("property_name", "currency")}
        res = InHouseReservation(
            hotel_name=property_store.get(property_code),
            currency=property_store.get_currency(property_code),
            **fields,
        )
        balance_threshold, _ = balance_rules.get_threshold(property_code)
        res.evaluate(balance_threshold, rooms_threshold)
        reservations.append(res)
    return reservations
