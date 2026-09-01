from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
import unittest

from inhouse_logic import InHouseReservation


def _reservation(*, departure_date: date, balance: str, rooms: int = 1) -> InHouseReservation:
    return InHouseReservation(
        property_code="AMSEM",
        hotel_name="Amsterdam",
        currency="EUR",
        confirmation_number="confirmation",
        reservation_id="reservation",
        status="CHECKED IN",
        rate_code="RATE",
        arrival_date=departure_date - timedelta(days=1),
        departure_date=departure_date,
        balance=Decimal(balance),
        number_of_rooms=rooms,
        source_description="Source",
        is_shiji=False,
        shiji_number=None,
        ta_locator=None,
        entity_lookup="a" * 64,
        finding_fingerprint="b" * 64,
    )


class InHouseReservationEvaluationTests(unittest.TestCase):
    def test_departing_today_with_zero_balance_is_not_flagged(self) -> None:
        reservation = _reservation(departure_date=date.today(), balance="0", rooms=2)

        reservation.evaluate(balance_threshold=-70, rooms_threshold=1)

        self.assertFalse(reservation.is_flagged)
        self.assertEqual(reservation.flag_reasons, [])
        self.assertIsNone(reservation.severity)

    def test_departing_today_with_nonzero_balance_uses_existing_balance_rules(self) -> None:
        reservation = _reservation(departure_date=date.today(), balance="10")

        reservation.evaluate(balance_threshold=-70, rooms_threshold=1)

        self.assertTrue(reservation.is_flagged)
        self.assertEqual(
            reservation.flag_reasons,
            [{"label": "Positive balance", "severity": "danger"}],
        )

    def test_zero_balance_still_flags_before_departure_day(self) -> None:
        reservation = _reservation(departure_date=date.today() + timedelta(days=1), balance="0")

        reservation.evaluate(balance_threshold=-70, rooms_threshold=1)

        self.assertTrue(reservation.is_flagged)
        self.assertEqual(
            reservation.flag_reasons,
            [{"label": "Zero balance", "severity": "warning"}],
        )


if __name__ == "__main__":
    unittest.main()
