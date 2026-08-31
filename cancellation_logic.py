"""Business logic for the cancellation check task.

Reads currently-known cancellations from the `cancellations` table and
decides, per reservation, whether the cancellation was made late enough
(relative to its cancellation policy) that a fee needs to be booked, and if
so what the fee should be based on.

The table is populated by an external ingestion pipeline, not by Alfred -
see dedup.py for the shared dedup logic it's expected to use before writing
rows here (one row per reservation, already collapsed from the PMS export's
one-row-per-external-reference-type shape).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

from dedup import first_non_empty_by_property
from pms import load_cancellation_events
from property_store import PropertyStore
from rules_store import RuleStore

OPERA_BOOKMARK_URL = (
    "https://ihgce1.oraclehospitality.eu-frankfurt-1.ocs.oraclecloud.com/"
    "IHGENT/operacloud/bookmarks/reservation?resvId={resv_id}&TPRESORT={property_code}"
)


@dataclass
class Cancellation:
    property_code: str
    hotel_name: str
    confirmation_number: str
    reservation_id: str
    status: str
    cancel_date: date
    cancel_time: time
    arrival_date: date
    rule_text: str
    rate_code: str
    nights: int
    source_description: str
    is_shiji: bool
    shiji_number: str | None
    ta_locator: str | None
    entity_lookup: str
    finding_fingerprint: str

    is_late: bool = field(init=False, default=False)
    fee_type: str | None = field(init=False, default=None)
    days_prior: int = field(init=False, default=0)
    reason: str = field(init=False, default="")
    deadline_text: str = field(init=False, default="")
    configured: bool = field(init=False, default=True)
    handled: bool = field(init=False, default=False)
    handled_at: str | None = field(init=False, default=None)

    def evaluate(self, rule_store: RuleStore) -> None:
        rule = rule_store.get(self.rule_text)
        self.days_prior = (self.arrival_date - self.cancel_date).days
        self.configured = rule.get("configured", False)
        self.fee_type = rule.get("fee_type")

        if not self.configured:
            self.is_late = False
            self.reason = f'Cancellation policy "{self.rule_text}" is not configured yet - see Settings.'
            self.deadline_text = ""
            return

        if rule.get("always_late"):
            self.is_late = True
            self.reason = "Non-refundable rate - no free cancellation window."
            self.deadline_text = "No free cancellation"
            return

        threshold = rule.get("days_before_arrival") or 0
        cutoff = rule.get("cutoff_time")
        deadline_date = self.arrival_date - timedelta(days=threshold)

        if cutoff:
            # A cutoff time applies to the deadline day itself, however many
            # days before arrival that falls - "1st N 1D Prior" at 18:00 means
            # free until 18:00 the day before arrival, not just until midnight.
            deadline_display = f"{deadline_date.strftime('%d %b')}, {cutoff}"
            deadline_dt = datetime.combine(deadline_date, datetime.strptime(cutoff, "%H:%M").time())
            self.is_late = datetime.combine(self.cancel_date, self.cancel_time) > deadline_dt
        else:
            # No time of day configured yet - only the date matters, and the
            # deadline day itself still counts as free.
            deadline_display = deadline_date.strftime("%d %b")
            self.is_late = self.cancel_date > deadline_date

        self.deadline_text = f"by {deadline_display}"
        self.reason = (
            f"{'Cancelled after' if self.is_late else 'Cancelled before'} the {deadline_display} deadline."
        )

    @property
    def key(self) -> str:
        return self.entity_lookup

    @property
    def opera_url(self) -> str:
        return OPERA_BOOKMARK_URL.format(resv_id=self.reservation_id, property_code=self.property_code)


def load_cancellations(rule_store: RuleStore, property_store: PropertyStore) -> list[Cancellation]:
    records = []
    for payload, entity_lookup, fingerprint in load_cancellation_events():
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
                    "cancel_date": date.fromisoformat(str(payload["cancellation_date"])),
                    "cancel_time": time.fromisoformat(str(payload["cancellation_time"])),
                    "arrival_date": date.fromisoformat(str(payload["arrival_date"])),
                    "rule_text": str(payload["cancellation_rule"]),
                    "rate_code": str(payload.get("rate_code", "")),
                    "nights": int(payload.get("nights", 0)),
                    "source_description": str(payload.get("source_description", "")),
                    "is_shiji": "SHIJI" in ref_map,
                    "shiji_number": ref_map.get("SHIJI") or None,
                    "ta_locator": ref_map.get("TA_RECORD_LOCATOR") or None,
                    "property_name": str(payload.get("property_name", "")),
                    "currency": str(payload.get("currency", "")),
                    "entity_lookup": entity_lookup,
                    "finding_fingerprint": fingerprint,
                }
            )
        except (KeyError, TypeError, ValueError):
            continue

    rule_store.ensure_known({r["rule_text"] for r in records})
    property_store.ensure_known_names(first_non_empty_by_property(records, "property_code", "property_name"))
    property_store.ensure_known_currencies(first_non_empty_by_property(records, "property_code", "currency"))

    cancellations = []
    for r in records:
        fields = {k: v for k, v in r.items() if k not in ("property_name", "currency")}
        c = Cancellation(hotel_name=property_store.get(r["property_code"]), **fields)
        c.evaluate(rule_store)
        cancellations.append(c)
    return cancellations
