import unittest
from datetime import date, datetime, timezone

from portfolio_app.time_utils import current_valuation_date


class TimeUtilsTest(unittest.TestCase):
    def test_current_valuation_date_uses_china_standard_time(self):
        instant = datetime(2026, 9, 7, 16, 30, tzinfo=timezone.utc)

        self.assertEqual(current_valuation_date(instant), date(2026, 9, 8))

    def test_naive_input_is_treated_as_utc(self):
        instant = datetime(2026, 9, 7, 16, 30)

        self.assertEqual(current_valuation_date(instant), date(2026, 9, 8))


if __name__ == "__main__":
    unittest.main()
