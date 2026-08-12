import unittest

from datetime import date
from unittest.mock import Mock

from portfolio_app.market_data import (
    _eastmoney_fund_code_for_instrument,
    _fetch_eastmoney_fund_history,
    _instrument_query,
    _parse_jsonp_payload,
    _resolve_incremental_start,
    _sina_symbol_for_instrument,
    _yahoo_symbol_for_instrument,
)


class MarketDataTest(unittest.TestCase):
    def test_eastmoney_fund_history_paginates_structured_nav_rows(self):
        first_response = Mock()
        first_response.raise_for_status.return_value = None
        first_response.json.return_value = {
            "Data": {
                "LSJZList": [
                    {"FSRQ": "2025-07-16", "DWJZ": "2.8039"},
                    {"FSRQ": "2025-07-15", "DWJZ": "2.8169"},
                ]
            },
            "ErrCode": 0,
            "TotalCount": 3,
            "PageSize": 2,
        }
        second_response = Mock()
        second_response.raise_for_status.return_value = None
        second_response.json.return_value = {
            "Data": {"LSJZList": [{"FSRQ": "2025-07-14", "DWJZ": "2.8217"}]},
            "ErrCode": 0,
            "TotalCount": 3,
            "PageSize": 2,
        }
        session = Mock()
        session.get.side_effect = [first_response, second_response]

        rows = _fetch_eastmoney_fund_history(
            session,
            {"id": "gtja:000218", "currency": "CNY"},
            "000218",
            start_date="2025-03-18",
        )

        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[-1]["price_date"], "2025-07-14")
        self.assertEqual(rows[-1]["close_price"], 2.8217)
        self.assertEqual(session.get.call_count, 2)
        self.assertEqual(session.get.call_args_list[1].kwargs["params"]["pageIndex"], 2)

    def test_eastmoney_fund_code_for_off_exchange_cn_fund(self):
        self.assertEqual(
            _eastmoney_fund_code_for_instrument(
                {
                    "broker": "gtja",
                    "market": "CN",
                    "asset_class": "fund",
                    "symbol": "002362",
                    "has_confirm": 1,
                }
            ),
            "002362",
        )

    def test_eastmoney_fund_code_skips_exchange_traded_and_non_funds(self):
        self.assertIsNone(
            _eastmoney_fund_code_for_instrument(
                {
                    "broker": "gtja",
                    "market": "CN",
                    "asset_class": "fund",
                    "symbol": "518800",
                    "has_confirm": 1,
                }
            )
        )
        self.assertIsNone(
            _eastmoney_fund_code_for_instrument(
                {
                    "broker": "gtja",
                    "market": "CN",
                    "asset_class": "cash_management",
                    "symbol": "952100",
                    "has_confirm": 0,
                }
            )
        )
        self.assertIsNone(
            _eastmoney_fund_code_for_instrument(
                {
                    "broker": "gtja",
                    "market": "CN",
                    "asset_class": "fund",
                    "symbol": "850011",
                    "has_confirm": 0,
                }
            )
        )

    def test_parse_jsonp_payload(self):
        payload = _parse_jsonp_payload('jQuery({"Data":[[1725494400000.0,1.215]],"ErrCode":0,"ErrMsg":null});')
        self.assertEqual(payload["ErrCode"], 0)
        self.assertEqual(payload["Data"][0][1], 1.215)

    def test_sina_symbol_for_cn_market_instrument(self):
        self.assertEqual(
            _sina_symbol_for_instrument(
                {
                    "broker": "gtja",
                    "market": "CN",
                    "symbol": "561990",
                }
            ),
            "sh561990",
        )
        self.assertEqual(
            _sina_symbol_for_instrument(
                {
                    "broker": "gtja",
                    "market": "CN",
                    "symbol": "000001",
                }
            ),
            "sz000001",
        )

    def test_yahoo_symbol_for_ibkr_security(self):
        self.assertEqual(
            _yahoo_symbol_for_instrument(
                {
                    "broker": "ibkr",
                    "symbol": "QQQM",
                    "currency": "USD",
                    "market": "NASDAQ",
                    "asset_class": "security",
                }
            ),
            "QQQM",
        )

    def test_instrument_query_injects_target_filter_before_order_by(self):
        query, params = _instrument_query(
            ["ibkr:qqqm", "gtja:561990"],
            """
            SELECT DISTINCT id, symbol
            FROM instruments
            WHERE asset_class IN ('equity', 'etf')
            ORDER BY symbol
            """,
        )
        self.assertIn("AND id IN (?, ?)", query)
        self.assertTrue(query.index("AND id IN (?, ?)") < query.index("ORDER BY symbol"))
        self.assertEqual(params, ["ibkr:qqqm", "gtja:561990"])

    def test_instrument_query_respects_custom_id_column(self):
        query, params = _instrument_query(
            ["gtja:002362"],
            """
            SELECT i.id, i.symbol
            FROM instruments i
            WHERE i.asset_class = 'fund'
            ORDER BY i.symbol
            """,
            id_column="i.id",
        )
        self.assertIn("AND i.id IN (?)", query)
        self.assertEqual(params, ["gtja:002362"])

    def test_resolve_incremental_start_prefers_latest_known_date_with_padding(self):
        self.assertEqual(
            _resolve_incremental_start(
                (date(2026, 1, 1), date(2026, 5, 10)),
                (date(2026, 2, 1), date(2026, 5, 12)),
                fallback_days=365,
                padding_days=10,
            ),
            date(2026, 5, 2),
        )

    def test_resolve_incremental_start_backfills_historical_gap_from_transaction_start(self):
        self.assertEqual(
            _resolve_incremental_start(
                (date(2025, 12, 24), date(2026, 5, 15)),
                transaction_start=date(2025, 7, 17),
                fallback_days=365,
                padding_days=10,
            ),
            date(2025, 7, 7),
        )


if __name__ == "__main__":
    unittest.main()
