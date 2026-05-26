import unittest
from datetime import datetime, timedelta, timezone

from portfolio_app.server import PortfolioApplication, _format_sync_time, _is_sync_stale


class ServerHelpersTest(unittest.TestCase):
    def test_current_holding_instrument_ids_only_returns_open_products(self):
        dashboard = {
            "products": [
                {"instrument_id": "b", "status": "closed", "quantity": 0, "market_value_base": 100},
                {"instrument_id": "a", "status": "open", "quantity": 2, "market_value_base": 300},
                {"instrument_id": "c", "status": "open", "quantity": 1, "market_value_base": 120},
            ]
        }
        self.assertEqual(PortfolioApplication._current_holding_instrument_ids(dashboard), ["a", "c"])

    def test_is_sync_stale_respects_ttl(self):
        recent = (datetime.now(timezone.utc) - timedelta(minutes=5)).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        old = (datetime.now(timezone.utc) - timedelta(minutes=45)).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        self.assertFalse(_is_sync_stale(recent, timedelta(minutes=15)))
        self.assertTrue(_is_sync_stale(old, timedelta(minutes=15)))

    def test_format_sync_time_renders_local_string(self):
        rendered = _format_sync_time("2026-05-14T08:30:00Z")
        self.assertRegex(rendered, r"2026-05-14 \d{2}:\d{2}:\d{2}")


if __name__ == "__main__":
    unittest.main()
