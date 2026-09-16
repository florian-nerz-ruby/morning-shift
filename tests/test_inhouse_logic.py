from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
import unittest
from unittest.mock import patch

from inhouse_logic import InHouseReservation, load_inhouse_reservations


def _reservation(
    *, departure_date: date, balance: str, rooms: int = 1, failed_deposit_errors: tuple[str, ...] = ()
) -> InHouseReservation:
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
        failed_deposit_errors=failed_deposit_errors,
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

    def test_failed_deposit_is_a_danger_flag_even_on_departure_day(self) -> None:
        reservation = _reservation(
            departure_date=date.today(),
            balance="0",
            rooms=2,
            failed_deposit_errors=("Invalid Card Number",),
        )

        reservation.evaluate(balance_threshold=-70, rooms_threshold=1)

        self.assertTrue(reservation.is_flagged)
        self.assertEqual(reservation.severity, "danger")
        self.assertEqual(
            reservation.flag_reasons,
            [
                {
                    "label": "Deposit failed",
                    "severity": "danger",
                    "detail": "Invalid Card Number",
                }
            ],
        )
        self.assertFalse(reservation.appears_in_inhouse_check)

    def test_failed_deposit_with_another_flag_stays_in_inhouse_check(self) -> None:
        reservation = _reservation(
            departure_date=date.today() + timedelta(days=1),
            balance="0",
            failed_deposit_errors=("Invalid Card Number",),
        )

        reservation.evaluate(balance_threshold=-70, rooms_threshold=1)

        self.assertTrue(reservation.appears_in_inhouse_check)


class InHouseReservationLoadingTests(unittest.TestCase):
    def test_loads_distinct_deposit_errors_from_encrypted_payload(self) -> None:
        class PropertyStore:
            def ensure_known_names(self, _names) -> None:
                pass

            def ensure_known_currencies(self, _currencies) -> None:
                pass

            def get(self, _property_code: str) -> str:
                return "Amsterdam"

            def get_currency(self, _property_code: str) -> str:
                return "EUR"

        class BalanceRules:
            def get_threshold(self, _property_code: str) -> tuple[float, None]:
                return -70.0, None

        payload = {
            "property_code": "AMSEM",
            "confirmation_number": "confirmation",
            "reservation_id": "reservation",
            "status": "CHECKED IN",
            "rate_code": "RATE",
            "arrival_date": "2026-09-15",
            "departure_date": "2026-09-17",
            "balance": {"currency": "EUR", "amount_minor": -100},
            "number_of_rooms": 1,
            "failed_deposit_errors": ["Declined", "declined", "Invalid Card Number", 42],
        }

        with patch("inhouse_logic.load_current_inhouse", return_value=[(payload, "a" * 64, "b" * 64)]):
            rows = load_inhouse_reservations(PropertyStore(), BalanceRules(), rooms_threshold=1)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].failed_deposit_errors, ("Declined", "Invalid Card Number"))
        self.assertTrue(rows[0].is_flagged)
        self.assertEqual(rows[0].flag_reasons[0]["label"], "Deposit failed")


if __name__ == "__main__":
    unittest.main()
