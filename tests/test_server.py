import sqlite3
import unittest
from datetime import datetime, timedelta, timezone

from portfolio_app import db
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

    def test_historical_instruments_without_market_prices_are_backfilled(self):
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.executescript(db.SCHEMA)
        connection.execute(
            """
            INSERT INTO accounts (id, broker, account_code, display_name, base_currency)
            VALUES ('account', 'gtja', 'account', 'Account', 'CNY')
            """
        )
        instruments = [
            ("gtja:561990", "gtja", "561990", "Missing ETF", "etf", "CN", "CNY"),
            ("gtja:518800", "gtja", "518800", "Priced ETF", "etf", "CN", "CNY"),
            ("gtja:004475", "gtja", "004475", "Missing Fund", "fund", "CN", "CNY"),
            ("ibkr:NVDA", "ibkr", "NVDA", "Missing Equity", "equity", "US", "USD"),
            ("gtja:cash", "gtja", "cash", "Cash", "cash_management", "CN", "CNY"),
        ]
        connection.executemany(
            """
            INSERT INTO instruments (id, broker, symbol, name, asset_class, market, currency)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            instruments,
        )
        connection.executemany(
            """
            INSERT INTO transactions (
                id, broker, account_id, instrument_id, settle_date, trade_date,
                activity_type, description, currency
            ) VALUES (?, ?, 'account', ?, '2026-01-01', '2026-01-01', 'security_buy', 'Buy', ?)
            """,
            [
                (f"tx-{index}", broker, instrument_id, currency)
                for index, (instrument_id, broker, _symbol, _name, _class, _market, currency)
                in enumerate(instruments)
            ],
        )
        connection.execute(
            """
            INSERT INTO price_history (instrument_id, price_date, close_price, currency, source)
            VALUES ('gtja:518800', '2026-01-01', 9.5, 'CNY', 'sina_kline')
            """
        )

        self.assertEqual(
            PortfolioApplication._historical_instrument_ids_missing_market_prices(connection),
            ["gtja:004475", "gtja:561990", "ibkr:NVDA"],
        )
        connection.close()

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
