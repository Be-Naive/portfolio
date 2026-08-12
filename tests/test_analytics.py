import json
import sqlite3
import unittest
from datetime import date, timedelta

from portfolio_app import db
from portfolio_app.analytics import (
    _build_timeseries,
    _benchmark_catalog,
    _rebuild_holdings,
    _display_label,
    _effective_bridge_cash_delta,
    _filter_bootstrap_price_rows,
    _latest_cash_balances,
    _modified_dietz_return,
    _product_analysis,
    _cash_effects,
    _enrich_fund_flow_row,
    _resolve_fx_rate,
    _transaction_effect,
    build_dashboard,
    parse_rebalance_targets,
    suggest_rebalance,
)


class AnalyticsTest(unittest.TestCase):
    def test_dashboard_headline_matches_timeseries_endpoint(self):
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.executescript(db.SCHEMA)
        today = date.today().isoformat()
        connection.execute(
            """
            INSERT INTO accounts (id, broker, account_code, display_name, base_currency)
            VALUES ('account', 'test', 'account', 'Test account', 'CNY')
            """
        )
        connection.execute(
            """
            INSERT INTO instruments (id, broker, symbol, name, asset_class, market, currency)
            VALUES ('test:asset', 'test', 'ASSET', 'Asset', 'equity', 'CN', 'CNY')
            """
        )
        connection.executemany(
            """
            INSERT INTO transactions (
                id, broker, account_id, instrument_id, settle_date, trade_date,
                activity_type, description, external_flow, quantity, price,
                gross_amount, cash_amount, currency
            ) VALUES (?, 'test', 'account', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'CNY')
            """,
            [
                ("flow", None, today, today, "bank_transfer_in", "Deposit", 1, None, None, None, 1000.0),
                ("buy", "test:asset", today, today, "security_buy", "Buy", 0, 3.0, 100.0, 300.0, -300.0),
            ],
        )
        connection.execute(
            """
            INSERT INTO price_history (instrument_id, price_date, close_price, currency, source)
            VALUES ('test:asset', ?, 101.005, 'CNY', 'manual_csv')
            """,
            (today,),
        )

        dashboard = build_dashboard(connection)

        self.assertEqual(dashboard["summary"]["total_market_value"], dashboard["timeseries"]["nav"][-1]["value"])
        self.assertEqual(dashboard["summary"]["total_return"], dashboard["timeseries"]["profit"][-1]["value"])
        connection.close()

    def test_empty_timeseries_preserves_chart_series_shape(self):
        self.assertEqual(
            _build_timeseries([], [], [], "CNY"),
            {
                "nav": [],
                "net_contribution": [],
                "total_twr": [],
                "effective_twr": [],
                "peak_cost_rate": [],
                "product_profit_breakdown": [],
            },
        )

    def test_timeseries_product_profit_breakdown_reconciles_to_daily_profit(self):
        trade_date = (date.today() - timedelta(days=1)).isoformat()
        price_date = date.today().isoformat()
        transactions = [
            {
                "trade_date": trade_date,
                "activity_type": "security_buy",
                "description": "Test holding",
                "external_flow": 0,
                "quantity": 10.0,
                "price": 10.0,
                "gross_amount": 100.0,
                "cash_amount": -100.0,
                "position_balance": None,
                "currency": "CNY",
                "instrument_id": "test:holding",
                "account_id": "acct",
                "asset_class": "equity",
                "name": "测试产品",
                "symbol": "TEST",
                "other_fee": None,
            }
        ]
        price_rows = [
            {
                "price_date": trade_date,
                "instrument_id": "test:holding",
                "close_price": 10.0,
                "currency": "CNY",
                "asset_class": "equity",
            },
            {
                "price_date": price_date,
                "instrument_id": "test:holding",
                "close_price": 12.0,
                "currency": "CNY",
                "asset_class": "equity",
            },
        ]

        series = _build_timeseries(transactions, price_rows, [], "CNY")
        breakdown = {item["date"]: item for item in series["product_profit_breakdown"]}

        self.assertEqual(breakdown[trade_date]["total"], 0.0)
        self.assertEqual(breakdown[price_date]["total"], 20.0)
        self.assertEqual(
            breakdown[price_date]["items"],
            [
                {
                    "instrument_id": "test:holding",
                    "label": "测试产品 (TEST)",
                    "asset_class": "equity",
                    "value": 20.0,
                    "kind": "product",
                }
            ],
        )
        self.assertEqual(sum(item["value"] for item in breakdown[price_date]["items"]), breakdown[price_date]["total"])

    def test_parse_targets(self):
        parsed = parse_rebalance_targets("equity: 40\nfund=35\ncash 25")
        self.assertEqual(parsed["equity"], 40.0)
        self.assertEqual(parsed["fund"], 35.0)
        self.assertEqual(parsed["cash"], 25.0)

    def test_suggest_rebalance(self):
        allocations = [
            {"label": "equity", "market_value_base": 700.0, "weight": 0.7},
            {"label": "bond", "market_value_base": 200.0, "weight": 0.2},
            {"label": "cash", "market_value_base": 100.0, "weight": 0.1},
        ]
        result = suggest_rebalance(allocations, available_cash_base=100.0, total_value_base=1000.0, targets={"equity": 50, "bond": 30, "cash": 20})
        self.assertTrue(any(action["bucket"] == "bond" for action in result["actions"]))

    def test_suggest_rebalance_uses_selected_subset_total(self):
        allocations = [
            {"label": "黄金ETF (518800)", "market_value_base": 600.0, "weight": 0.6},
            {"label": "华泰复利 (004475)", "market_value_base": 400.0, "weight": 0.4},
        ]
        result = suggest_rebalance(
            allocations,
            available_cash_base=999.0,
            total_value_base=9999.0,
            targets={"黄金ETF (518800)": 50, "华泰复利 (004475)": 50},
        )
        self.assertEqual(result["subset_total"], 1000.0)
        drift_by_label = {item["label"]: item for item in result["drift"]}
        self.assertEqual(drift_by_label["黄金ETF (518800)"]["delta_value"], -100.0)
        self.assertEqual(drift_by_label["华泰复利 (004475)"]["delta_value"], 100.0)

    def test_display_label_combines_name_and_symbol(self):
        self.assertEqual(_display_label("黄金ETF", "518800"), "黄金ETF (518800)")
        self.assertEqual(_display_label("QQQM", "QQQM"), "QQQM")

    def test_benchmark_catalog_excludes_cash_management_and_keeps_price_series(self):
        products = [
            {
                "instrument_id": "ibkr:allw",
                "display_label": "ALLW",
                "asset_class": "equity",
                "price_currency": "USD",
                "status": "open",
            },
            {
                "instrument_id": "gtja:952100",
                "display_label": "国泰海通现金管家 (952100)",
                "asset_class": "cash_management",
                "price_currency": "CNY",
                "status": "open",
            },
        ]
        price_rows = [
            {"instrument_id": "ibkr:allw", "price_date": "2026-05-01", "close_price": 28.1, "currency": "USD", "source": "yahoo_chart"},
            {"instrument_id": "ibkr:allw", "price_date": "2026-05-02", "close_price": 28.4, "currency": "USD", "source": "yahoo_chart"},
            {"instrument_id": "gtja:952100", "price_date": "2026-05-01", "close_price": 1.0, "currency": "CNY", "source": "gtja_transaction"},
            {"instrument_id": "gtja:952100", "price_date": "2026-05-02", "close_price": 1.0, "currency": "CNY", "source": "gtja_transaction"},
        ]
        choices, series = _benchmark_catalog(products, price_rows)
        self.assertEqual(len(choices), 1)
        self.assertEqual(choices[0]["id"], "ibkr:allw")
        self.assertIn("ibkr:allw", series)
        self.assertNotIn("gtja:952100", series)

    def test_benchmark_catalog_prefers_adjusted_series_when_available(self):
        products = [
            {
                "instrument_id": "gtja:000001",
                "display_label": "平安银行 (000001)",
                "asset_class": "equity",
                "price_currency": "CNY",
                "status": "open",
            }
        ]
        price_rows = [
            {"instrument_id": "gtja:000001", "price_date": "2025-10-13", "close_price": 11.81, "currency": "CNY", "source": "sina_kline"},
            {"instrument_id": "gtja:000001", "price_date": "2025-10-13", "close_price": 11.12, "currency": "CNY", "source": "yahoo_chart_adjusted"},
            {"instrument_id": "gtja:000001", "price_date": "2025-10-13", "close_price": 11.79, "currency": "CNY", "source": "yahoo_chart_close"},
            {"instrument_id": "gtja:000001", "price_date": "2025-10-14", "close_price": 11.30, "currency": "CNY", "source": "yahoo_chart_adjusted"},
        ]
        _choices, series = _benchmark_catalog(products, price_rows)
        self.assertEqual(series["gtja:000001"]["series"][0]["value"], 11.12)

    def test_filter_bootstrap_price_rows_drops_trade_bootstrap_after_market_prices_exist(self):
        rows = [
            {"instrument_id": "ibkr:qqqm", "price_date": "2026-05-22", "close_price": 248.769, "currency": "USD", "source": "ibkr_flex_trade"},
            {"instrument_id": "ibkr:qqqm", "price_date": "2026-05-22", "close_price": 295.44, "currency": "USD", "source": "yahoo_chart_close"},
            {"instrument_id": "ibkr:qqqm", "price_date": "2026-05-25", "close_price": 248.769, "currency": "USD", "source": "ibkr_flex_trade"},
            {"instrument_id": "gtja:518800", "price_date": "2025-07-17", "close_price": 7.318, "currency": "CNY", "source": "gtja_transaction"},
            {"instrument_id": "gtja:518800", "price_date": "2025-12-24", "close_price": 9.524, "currency": "CNY", "source": "sina_kline"},
        ]
        filtered = _filter_bootstrap_price_rows(rows)
        remaining = {(row["instrument_id"], row["price_date"], row["source"]) for row in filtered}
        self.assertNotIn(("ibkr:qqqm", "2026-05-25", "ibkr_flex_trade"), remaining)
        self.assertNotIn(("ibkr:qqqm", "2026-05-22", "ibkr_flex_trade"), remaining)
        self.assertIn(("ibkr:qqqm", "2026-05-22", "yahoo_chart_close"), remaining)
        self.assertIn(("gtja:518800", "2025-07-17", "gtja_transaction"), remaining)
        self.assertIn(("gtja:518800", "2025-12-24", "sina_kline"), remaining)

    def test_product_analysis_exposes_native_and_base_currency_views(self):
        holdings = {
            "ibkr:qqqm": {
                "instrument_id": "ibkr:qqqm",
                "symbol": "QQQM",
                "name": "QQQM",
                "asset_class": "security",
                "market": "NASDAQ",
                "currency": "USD",
                "quantity": 10.0,
                "average_cost": 100.0,
                "cost_basis_total": 1000.0,
                "realized_pnl": 200.0,
                "account_name": "ibkr",
                "market_price": 110.0,
                "market_value": 1100.0,
                "unrealized_pnl": 100.0,
            }
        }
        price_map = {"ibkr:qqqm": (110.0, "USD")}
        fx_pairs = {("USD", "CNY"): 7.2}
        products = _product_analysis(holdings, price_map, fx_pairs, "CNY")
        self.assertEqual(len(products), 1)
        product = products[0]
        self.assertEqual(product["native_currency"], "USD")
        self.assertEqual(product["market_value"], 1100.0)
        self.assertEqual(product["market_value_base"], 7920.0)
        self.assertEqual(product["realized_pnl"], 200.0)
        self.assertEqual(product["realized_pnl_base"], 1440.0)
        self.assertEqual(product["unrealized_pnl"], 100.0)
        self.assertEqual(product["unrealized_pnl_base"], 720.0)
        self.assertEqual(product["total_return"], 300.0)
        self.assertEqual(product["total_return_base"], 2160.0)

    def test_product_analysis_revalues_snapshot_with_latest_market_price(self):
        holdings = {
            "ibkr:qqqm": {
                "instrument_id": "ibkr:qqqm",
                "symbol": "QQQM",
                "name": "QQQM",
                "asset_class": "security",
                "market": "NASDAQ",
                "currency": "USD",
                "quantity": 10.0,
                "average_cost": 100.0,
                "cost_basis_total": 1000.0,
                "realized_pnl": 0.0,
                "account_name": "ibkr",
                "market_price": 110.0,
                "market_value": 1100.0,
                "unrealized_pnl": 100.0,
            }
        }

        product = _product_analysis(
            holdings,
            {"ibkr:qqqm": (115.0, "USD")},
            {("USD", "CNY"): 7.2},
            "CNY",
        )[0]

        self.assertEqual(product["price"], 115.0)
        self.assertEqual(product["market_value"], 1150.0)
        self.assertEqual(product["market_value_base"], 8280.0)
        self.assertEqual(product["unrealized_pnl"], 150.0)

    def test_cash_effects_for_fx_conversion(self):
        row = {
            "activity_type": "fx_conversion",
            "quantity": 100.0,
            "cash_amount": -128.0,
            "currency": "SGD",
            "symbol": "USD.SGD",
            "commission_total": 2.0,
            "raw_json": json.dumps(
                {
                    "buySell": "BUY",
                    "symbol": "USD.SGD",
                    "ibCommissionCurrency": "USD",
                }
            ),
        }
        effects = _cash_effects(row)
        self.assertEqual(effects["USD"], 98.0)
        self.assertEqual(effects["SGD"], -128.0)

    def test_latest_cash_balances_ignores_base_summary_when_currency_detail_exists(self):
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.executescript(
            """
            CREATE TABLE accounts (id TEXT PRIMARY KEY, base_currency TEXT NOT NULL);
            CREATE TABLE cash_balances (
                account_id TEXT NOT NULL,
                snapshot_time TEXT NOT NULL,
                currency TEXT NOT NULL,
                amount REAL NOT NULL
            );
            INSERT INTO accounts VALUES ('ibkr:U1', 'USD');
            INSERT INTO cash_balances VALUES ('ibkr:U1', '2026-08-11T23:59:59Z', 'BASE_SUMMARY', 74.14);
            INSERT INTO cash_balances VALUES ('ibkr:U1', '2026-08-11T23:59:59Z', 'USD', 74.14);
            """
        )

        rows = _latest_cash_balances(connection)

        self.assertEqual([(row["currency"], row["amount"]) for row in rows], [("USD", 74.14)])
        connection.close()

    def test_latest_cash_balances_maps_lone_base_summary_to_account_currency(self):
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.executescript(
            """
            CREATE TABLE accounts (id TEXT PRIMARY KEY, base_currency TEXT NOT NULL);
            CREATE TABLE cash_balances (
                account_id TEXT NOT NULL,
                snapshot_time TEXT NOT NULL,
                currency TEXT NOT NULL,
                amount REAL NOT NULL
            );
            INSERT INTO accounts VALUES ('ibkr:U1', 'USD');
            INSERT INTO cash_balances VALUES ('ibkr:U1', '2026-08-11T23:59:59Z', 'BASE_SUMMARY', 74.14);
            """
        )

        rows = _latest_cash_balances(connection)

        self.assertEqual([(row["currency"], row["amount"]) for row in rows], [("USD", 74.14)])
        connection.close()

    def test_resolve_cross_fx_rate(self):
        pair_map = {
            ("SGD", "USD"): 0.79,
            ("CNY", "USD"): 0.138,
        }
        self.assertAlmostEqual(_resolve_fx_rate("SGD", "CNY", pair_map), 0.79 / 0.138, places=6)
        self.assertAlmostEqual(_resolve_fx_rate("USD", "CNY", pair_map), 1 / 0.138, places=6)

    def test_history_migration_has_no_cash_effect(self):
        self.assertEqual(
            _cash_effects(
                {
                    "activity_type": "history_migration",
                    "cash_amount": -50000.0,
                    "currency": "CNY",
                }
            ),
            {},
        )

    def test_enrich_fund_redemption_quantity_only_row(self):
        enriched = _enrich_fund_flow_row(
            {
                "activity_type": "fund_redemption_in",
                "instrument_id": "gtja:000218",
                "asset_class": "fund",
                "quantity": 100.0,
                "gross_amount": 0.0,
                "cash_amount": 0.0,
                "currency": "CNY",
                "price": None,
            },
            {"gtja:000218": (2.5, "CNY")},
        )
        self.assertEqual(enriched["gross_amount"], 250.0)
        self.assertEqual(enriched["price"], 2.5)

    def test_rebuild_holdings_realizes_fund_redemption_on_cash_settlement(self):
        transactions = [
            {
                "trade_date": "2025-03-28",
                "settle_date": "2025-03-28",
                "activity_type": "fund_subscription_confirm",
                "description": "基金申购确认",
                "external_flow": 0,
                "quantity": 3878.37,
                "price": 2.5784027826,
                "gross_amount": 10000.0,
                "cash_amount": None,
                "position_balance": None,
                "cash_balance": None,
                "currency": "CNY",
                "instrument_id": "gtja:000218",
                "account_id": "acct",
                "account_name": "acct",
                "symbol": "000218",
                "name": "000218",
                "asset_class": "fund",
                "market": "CN",
                "commission_total": None,
                "stamp_duty": None,
                "transfer_fee": None,
                "other_fee": None,
                "raw_json": "{}",
            },
            {
                "trade_date": "2025-04-08",
                "settle_date": "2025-04-08",
                "activity_type": "fund_subscription_confirm",
                "description": "基金申购确认",
                "external_flow": 0,
                "quantity": 5780.09,
                "price": 2.5951153010,
                "gross_amount": 15000.0,
                "cash_amount": None,
                "position_balance": None,
                "cash_balance": None,
                "currency": "CNY",
                "instrument_id": "gtja:000218",
                "account_id": "acct",
                "account_name": "acct",
                "symbol": "000218",
                "name": "000218",
                "asset_class": "fund",
                "market": "CN",
                "commission_total": None,
                "stamp_duty": None,
                "transfer_fee": None,
                "other_fee": None,
                "raw_json": "{}",
            },
            {
                "trade_date": "2025-07-14",
                "settle_date": "2025-07-14",
                "activity_type": "fund_redemption_in",
                "description": "基金赎回拨入",
                "external_flow": 0,
                "quantity": 9658.46,
                "price": None,
                "gross_amount": None,
                "cash_amount": None,
                "position_balance": None,
                "cash_balance": None,
                "currency": "CNY",
                "instrument_id": "gtja:000218",
                "account_id": "acct",
                "account_name": "acct",
                "symbol": "000218",
                "name": "000218",
                "asset_class": "fund",
                "market": "CN",
                "commission_total": None,
                "stamp_duty": None,
                "transfer_fee": None,
                "other_fee": 13.49,
                "raw_json": "{}",
            },
            {
                "trade_date": "2025-07-16",
                "settle_date": "2025-07-16",
                "activity_type": "fund_redemption_in",
                "description": "基金赎回拨入",
                "external_flow": 0,
                "quantity": None,
                "price": None,
                "gross_amount": None,
                "cash_amount": 26935.62,
                "position_balance": None,
                "cash_balance": None,
                "currency": "CNY",
                "instrument_id": "gtja:000218",
                "account_id": "acct",
                "account_name": "acct",
                "symbol": "000218",
                "name": "000218",
                "asset_class": "fund",
                "market": "CN",
                "commission_total": None,
                "stamp_duty": None,
                "transfer_fee": None,
                "other_fee": None,
                "raw_json": "{}",
            },
        ]
        holdings, _cash = _rebuild_holdings(transactions, [], [], {}, "CNY")
        bucket = holdings["gtja:000218"]
        self.assertEqual(bucket["quantity"], 0.0)
        self.assertAlmostEqual(bucket["realized_pnl"], 1935.62, places=2)

    def test_timeseries_settles_quantity_only_fund_redemption_without_double_counting(self):
        transactions = [
            {
                "trade_date": "2025-03-28",
                "activity_type": "fund_subscription_confirm",
                "external_flow": 0,
                "quantity": 9658.46,
                "price": 2.595115301,
                "gross_amount": 25063.0,
                "cash_amount": None,
                "position_balance": None,
                "currency": "CNY",
                "instrument_id": "gtja:000218",
                "account_id": "acct",
                "asset_class": "fund",
                "other_fee": None,
            },
            {
                "trade_date": "2025-07-14",
                "activity_type": "fund_redemption_in",
                "external_flow": 0,
                "quantity": 9658.46,
                "price": None,
                "gross_amount": None,
                "cash_amount": None,
                "position_balance": None,
                "currency": "CNY",
                "instrument_id": "gtja:000218",
                "account_id": "acct",
                "asset_class": "fund",
                "other_fee": 13.49,
            },
            {
                "trade_date": "2025-07-16",
                "activity_type": "fund_redemption_in",
                "external_flow": 0,
                "quantity": None,
                "price": None,
                "gross_amount": None,
                "cash_amount": 26935.62,
                "position_balance": None,
                "currency": "CNY",
                "instrument_id": "gtja:000218",
                "account_id": "acct",
                "asset_class": "fund",
                "other_fee": None,
            },
        ]

        price_rows = [
            {
                "price_date": "2025-07-11",
                "instrument_id": "gtja:000218",
                "close_price": 2.7902,
                "currency": "CNY",
                "asset_class": "fund",
            },
            {
                "price_date": "2025-07-14",
                "instrument_id": "gtja:000218",
                "close_price": 2.8217,
                "currency": "CNY",
                "asset_class": "fund",
            },
        ]

        series = _build_timeseries(transactions, price_rows, [], "CNY")
        nav = {point["date"]: point["value"] for point in series["nav"]}

        self.assertAlmostEqual(nav["2025-07-11"], round(9658.46 * 2.7902, 2), places=2)
        self.assertAlmostEqual(nav["2025-07-14"], 26935.62, places=2)
        self.assertAlmostEqual(nav["2025-07-15"], 26935.62, places=2)
        self.assertAlmostEqual(nav["2025-07-16"], 26935.62, places=2)
        self.assertAlmostEqual(nav["2025-07-16"] - nav["2025-07-15"], 0.0, places=2)

    def test_timeseries_does_not_match_a_smaller_redemption_to_a_pending_settlement(self):
        transactions = [
            {
                "trade_date": "2025-03-28",
                "activity_type": "fund_subscription_confirm",
                "external_flow": 0,
                "quantity": 42632.65,
                "price": 1.29,
                "gross_amount": 55000.0,
                "cash_amount": None,
                "position_balance": None,
                "currency": "CNY",
                "instrument_id": "gtja:002362",
                "account_id": "acct",
                "asset_class": "fund",
                "other_fee": None,
            },
            {
                "trade_date": "2026-02-13",
                "activity_type": "fund_redemption_in",
                "external_flow": 0,
                "quantity": 5457.0,
                "price": 1.343,
                "gross_amount": 7328.75,
                "cash_amount": None,
                "position_balance": 37175.65,
                "currency": "CNY",
                "instrument_id": "gtja:002362",
                "account_id": "acct",
                "asset_class": "fund",
                "other_fee": None,
            },
            {
                "trade_date": "2026-02-24",
                "activity_type": "fund_redemption_in",
                "external_flow": 0,
                "quantity": 37175.65,
                "price": 1.342,
                "gross_amount": 49889.72,
                "cash_amount": None,
                "position_balance": None,
                "currency": "CNY",
                "instrument_id": "gtja:002362",
                "account_id": "acct",
                "asset_class": "fund",
                "other_fee": None,
            },
            {
                "trade_date": "2026-02-25",
                "activity_type": "fund_redemption_in",
                "external_flow": 0,
                "quantity": 5457.0,
                "price": 1.343,
                "gross_amount": 7328.75,
                "cash_amount": 7328.75,
                "position_balance": None,
                "currency": "CNY",
                "instrument_id": "gtja:002362",
                "account_id": "acct",
                "asset_class": "fund",
                "other_fee": None,
            },
            {
                "trade_date": "2026-02-26",
                "activity_type": "fund_redemption_in",
                "external_flow": 0,
                "quantity": 37175.65,
                "price": 1.342,
                "gross_amount": 49889.72,
                "cash_amount": 49889.72,
                "position_balance": None,
                "currency": "CNY",
                "instrument_id": "gtja:002362",
                "account_id": "acct",
                "asset_class": "fund",
                "other_fee": None,
            },
        ]

        series = _build_timeseries(transactions, [], [], "CNY")
        nav = {point["date"]: point["value"] for point in series["nav"]}
        contribution = series["net_contribution"][-1]["value"]

        self.assertAlmostEqual(nav["2026-02-26"], 57218.47, places=2)
        self.assertAlmostEqual(contribution, 0.0, places=2)

    def test_cash_management_full_position_adjustment_out_clears_cost_basis(self):
        transactions = [
            {
                "trade_date": "2024-10-21",
                "settle_date": "2024-10-21",
                "activity_type": "fund_subscription_fund_out",
                "description": "基金申购拨出",
                "external_flow": 0,
                "quantity": 5995.0,
                "price": None,
                "gross_amount": None,
                "cash_amount": None,
                "position_balance": None,
                "cash_balance": None,
                "currency": "CNY",
                "instrument_id": "gtja:850011",
                "account_id": "acct",
                "account_name": "acct",
                "symbol": "850011",
                "name": "850011",
                "asset_class": "cash_management",
                "market": "CN",
                "commission_total": None,
                "stamp_duty": None,
                "transfer_fee": None,
                "other_fee": None,
                "raw_json": "{}",
            },
            {
                "trade_date": "2024-10-30",
                "settle_date": "2024-10-30",
                "activity_type": "position_adjustment_out",
                "description": "基金非交易过户出",
                "external_flow": 0,
                "quantity": 5995.0,
                "price": None,
                "gross_amount": None,
                "cash_amount": None,
                "position_balance": None,
                "cash_balance": None,
                "currency": "CNY",
                "instrument_id": "gtja:850011",
                "account_id": "acct",
                "account_name": "acct",
                "symbol": "850011",
                "name": "850011",
                "asset_class": "cash_management",
                "market": "CN",
                "commission_total": None,
                "stamp_duty": None,
                "transfer_fee": None,
                "other_fee": None,
                "raw_json": "{}",
            },
            {
                "trade_date": "2024-11-18",
                "settle_date": "2024-11-18",
                "activity_type": "position_adjustment",
                "description": "基金红股",
                "external_flow": 0,
                "quantity": 11.53,
                "price": None,
                "gross_amount": None,
                "cash_amount": None,
                "position_balance": None,
                "cash_balance": None,
                "currency": "CNY",
                "instrument_id": "gtja:850011",
                "account_id": "acct",
                "account_name": "acct",
                "symbol": "850011",
                "name": "850011",
                "asset_class": "cash_management",
                "market": "CN",
                "commission_total": None,
                "stamp_duty": None,
                "transfer_fee": None,
                "other_fee": None,
                "raw_json": "{}",
            },
            {
                "trade_date": "2024-11-19",
                "settle_date": "2024-11-19",
                "activity_type": "fund_redemption_in",
                "description": "基金赎回拨入",
                "external_flow": 0,
                "quantity": 11.53,
                "price": None,
                "gross_amount": None,
                "cash_amount": None,
                "position_balance": None,
                "cash_balance": None,
                "currency": "CNY",
                "instrument_id": "gtja:850011",
                "account_id": "acct",
                "account_name": "acct",
                "symbol": "850011",
                "name": "850011",
                "asset_class": "cash_management",
                "market": "CN",
                "commission_total": None,
                "stamp_duty": None,
                "transfer_fee": None,
                "other_fee": None,
                "raw_json": "{}",
            },
            {
                "trade_date": "2024-11-19",
                "settle_date": "2024-11-19",
                "activity_type": "fund_redemption_in",
                "description": "基金赎回拨入",
                "external_flow": 0,
                "quantity": None,
                "price": None,
                "gross_amount": None,
                "cash_amount": 11.53,
                "position_balance": None,
                "cash_balance": None,
                "currency": "CNY",
                "instrument_id": "gtja:850011",
                "account_id": "acct",
                "account_name": "acct",
                "symbol": "850011",
                "name": "850011",
                "asset_class": "cash_management",
                "market": "CN",
                "commission_total": None,
                "stamp_duty": None,
                "transfer_fee": None,
                "other_fee": None,
                "raw_json": "{}",
            },
        ]
        holdings, _cash = _rebuild_holdings(transactions, [], [], {}, "CNY")
        bucket = holdings["gtja:850011"]
        self.assertEqual(bucket["quantity"], 0.0)
        self.assertAlmostEqual(bucket["realized_pnl"], 11.53, places=2)

    def test_closed_position_with_realized_pnl_still_shows_up(self):
        products = _product_analysis(
            {
                "gtja:561990": {
                    "instrument_id": "gtja:561990",
                    "symbol": "561990",
                    "name": "300增强",
                    "asset_class": "etf",
                    "market": "CN",
                    "currency": "CNY",
                    "quantity": 0.0,
                    "average_cost": 0.0,
                    "cost_basis_total": 0.0,
                    "realized_pnl": 2932.76,
                    "account_name": "国泰海通 48530",
                }
            },
            {"gtja:561990": (1.073, "CNY")},
            {},
            "CNY",
        )
        self.assertEqual(len(products), 1)
        self.assertEqual(products[0]["status"], "closed")
        self.assertEqual(products[0]["market_value_base"], 0.0)
        self.assertEqual(products[0]["total_return"], 2932.76)

    def test_modified_dietz_return_neutralizes_flow(self):
        self.assertAlmostEqual(_modified_dietz_return(100.0, 120.0, 20.0), 0.0, places=8)
        self.assertAlmostEqual(_modified_dietz_return(100.0, 110.0, 0.0), 0.1, places=8)

    def test_effective_bridge_cash_delta_for_excluded_history_migration(self):
        self.assertEqual(
            _effective_bridge_cash_delta(
                {
                    "activity_type": "history_migration",
                    "asset_class": "repo",
                    "cash_amount": 50156.88,
                }
            ),
            50156.88,
        )
        self.assertEqual(
            _effective_bridge_cash_delta(
                {
                    "activity_type": "history_migration",
                    "asset_class": "etf",
                    "cash_amount": 50156.88,
                }
            ),
            0.0,
        )

    def test_effective_twr_ignores_excluded_asset_bridge_swings(self):
        transactions = [
            {
                "trade_date": "2024-10-20",
                "settle_date": "2024-10-20",
                "activity_type": "bank_transfer_in",
                "description": "银行转证券",
                "external_flow": 1,
                "quantity": None,
                "price": None,
                "gross_amount": None,
                "cash_amount": 80000.0,
                "position_balance": None,
                "cash_balance": 80000.0,
                "currency": "CNY",
                "instrument_id": None,
                "account_id": "acct",
                "account_name": "acct",
                "symbol": None,
                "name": None,
                "asset_class": None,
                "market": None,
                "raw_json": "{}",
            },
            {
                "trade_date": "2024-10-20",
                "settle_date": "2024-10-20",
                "activity_type": "security_buy",
                "description": "普通买入",
                "external_flow": 0,
                "quantity": 27000.0,
                "price": 0.85,
                "gross_amount": 22950.0,
                "cash_amount": -22950.0,
                "position_balance": 27000.0,
                "cash_balance": 57050.0,
                "currency": "CNY",
                "instrument_id": "gtja:561990",
                "account_id": "acct",
                "account_name": "acct",
                "symbol": "561990",
                "name": "ETF",
                "asset_class": "etf",
                "market": "CN",
                "raw_json": "{}",
            },
            {
                "trade_date": "2024-10-20",
                "settle_date": "2024-10-20",
                "activity_type": "repo_open",
                "description": "深圳报价融券回购",
                "external_flow": 0,
                "quantity": None,
                "price": None,
                "gross_amount": 50000.0,
                "cash_amount": -50000.0,
                "position_balance": None,
                "cash_balance": 7050.0,
                "currency": "CNY",
                "instrument_id": "gtja:132818",
                "account_id": "acct",
                "account_name": "acct",
                "symbol": "132818",
                "name": "repo",
                "asset_class": "repo",
                "market": "CN",
                "raw_json": "{}",
            },
            {
                "trade_date": "2024-10-21",
                "settle_date": "2024-10-21",
                "activity_type": "fund_subscription_fund_out",
                "description": "基金申购拨出",
                "external_flow": 0,
                "quantity": 5995.0,
                "price": None,
                "gross_amount": None,
                "cash_amount": None,
                "position_balance": None,
                "cash_balance": None,
                "currency": "CNY",
                "instrument_id": "gtja:850011",
                "account_id": "acct",
                "account_name": "acct",
                "symbol": "850011",
                "name": "现金宝",
                "asset_class": "cash_management",
                "market": "CN",
                "raw_json": "{}",
            },
            {
                "trade_date": "2024-10-21",
                "settle_date": "2024-10-21",
                "activity_type": "fund_subscription_fund_out",
                "description": "基金申购拨出",
                "external_flow": 0,
                "quantity": None,
                "price": None,
                "gross_amount": None,
                "cash_amount": -5995.0,
                "position_balance": None,
                "cash_balance": 1055.0,
                "currency": "CNY",
                "instrument_id": "gtja:850011",
                "account_id": "acct",
                "account_name": "acct",
                "symbol": "850011",
                "name": "现金宝",
                "asset_class": "cash_management",
                "market": "CN",
                "raw_json": "{}",
            },
            {
                "trade_date": "2024-10-23",
                "settle_date": "2024-10-23",
                "activity_type": "fund_subscription_fund_out",
                "description": "基金申购拨出",
                "external_flow": 0,
                "quantity": 50000.0,
                "price": None,
                "gross_amount": None,
                "cash_amount": None,
                "position_balance": None,
                "cash_balance": None,
                "currency": "CNY",
                "instrument_id": "gtja:850011",
                "account_id": "acct",
                "account_name": "acct",
                "symbol": "850011",
                "name": "现金宝",
                "asset_class": "cash_management",
                "market": "CN",
                "raw_json": "{}",
            },
            {
                "trade_date": "2024-10-23",
                "settle_date": "2024-10-23",
                "activity_type": "fund_subscription_fund_out",
                "description": "基金申购拨出",
                "external_flow": 0,
                "quantity": None,
                "price": None,
                "gross_amount": None,
                "cash_amount": -50000.0,
                "position_balance": None,
                "cash_balance": -48945.0,
                "currency": "CNY",
                "instrument_id": "gtja:850011",
                "account_id": "acct",
                "account_name": "acct",
                "symbol": "850011",
                "name": "现金宝",
                "asset_class": "cash_management",
                "market": "CN",
                "raw_json": "{}",
            },
            {
                "trade_date": "2024-10-23",
                "settle_date": "2024-10-23",
                "activity_type": "history_migration",
                "description": "历史数据迁移",
                "external_flow": 0,
                "quantity": None,
                "price": None,
                "gross_amount": 50000.0,
                "cash_amount": 50000.0,
                "position_balance": None,
                "cash_balance": 1055.0,
                "currency": "CNY",
                "instrument_id": "gtja:132818",
                "account_id": "acct",
                "account_name": "acct",
                "symbol": "132818",
                "name": "repo",
                "asset_class": "repo",
                "market": "CN",
                "raw_json": "{}",
            },
            {
                "trade_date": "2024-10-24",
                "settle_date": "2024-10-24",
                "activity_type": "repo_close",
                "description": "深圳报价融券购回",
                "external_flow": 0,
                "quantity": None,
                "price": None,
                "gross_amount": 50000.0,
                "cash_amount": 50000.0,
                "position_balance": None,
                "cash_balance": 1055.0,
                "currency": "CNY",
                "instrument_id": "gtja:132818",
                "account_id": "acct",
                "account_name": "acct",
                "symbol": "132818",
                "name": "repo",
                "asset_class": "repo",
                "market": "CN",
                "raw_json": "{}",
            },
            {
                "trade_date": "2024-10-24",
                "settle_date": "2024-10-24",
                "activity_type": "history_migration",
                "description": "历史数据迁移",
                "external_flow": 0,
                "quantity": None,
                "price": None,
                "gross_amount": 50000.0,
                "cash_amount": -50000.0,
                "position_balance": None,
                "cash_balance": -48945.0,
                "currency": "CNY",
                "instrument_id": "gtja:132818",
                "account_id": "acct",
                "account_name": "acct",
                "symbol": "132818",
                "name": "repo",
                "asset_class": "repo",
                "market": "CN",
                "raw_json": "{}",
            },
        ]
        price_rows = [
            {"instrument_id": "gtja:561990", "price_date": "2024-10-20", "close_price": 0.85, "currency": "CNY", "source": "sina_kline", "asset_class": "etf"},
            {"instrument_id": "gtja:561990", "price_date": "2024-10-21", "close_price": 0.849, "currency": "CNY", "source": "sina_kline", "asset_class": "etf"},
            {"instrument_id": "gtja:561990", "price_date": "2024-10-22", "close_price": 0.851, "currency": "CNY", "source": "sina_kline", "asset_class": "etf"},
            {"instrument_id": "gtja:561990", "price_date": "2024-10-23", "close_price": 0.852, "currency": "CNY", "source": "sina_kline", "asset_class": "etf"},
            {"instrument_id": "gtja:561990", "price_date": "2024-10-24", "close_price": 0.853, "currency": "CNY", "source": "sina_kline", "asset_class": "etf"},
        ]
        series = _build_timeseries(transactions, price_rows, [], "CNY")
        effective = {
            item["date"]: item["value"]
            for item in series["effective_twr"]
            if "2024-10-22" <= item["date"] <= "2024-10-24"
        }
        self.assertLess(abs(effective["2024-10-23"] - effective["2024-10-22"]), 2.0)
        self.assertLess(abs(effective["2024-10-24"] - effective["2024-10-23"]), 2.0)

    def test_repo_uses_principal_as_position_delta(self):
        open_effect = _transaction_effect(
            {
                "asset_class": "repo",
                "activity_type": "repo_open",
                "gross_amount": 212000.0,
                "cash_amount": -212003.18,
                "quantity": 2120.0,
            }
        )
        close_effect = _transaction_effect(
            {
                "asset_class": "repo",
                "activity_type": "repo_close",
                "gross_amount": 212000.0,
                "cash_amount": 212040.8,
                "quantity": 2120.0,
            }
        )
        self.assertEqual(open_effect.quantity_delta, 212000.0)
        self.assertEqual(close_effect.quantity_delta, -212000.0)


if __name__ == "__main__":
    unittest.main()
