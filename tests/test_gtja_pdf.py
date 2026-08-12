import os
from pathlib import Path
from dataclasses import replace
import unittest

from portfolio_app.gtja_pdf import (
    ParsedGtjaRow,
    _activity_type_for_business,
    _classify_asset_class,
    _row_fingerprint,
    _transaction_id_for_row,
    parse_gtja_statement,
)


SAMPLE = Path(os.environ.get("GTJA_SAMPLE_PDF", Path(__file__).parent / "fixtures" / "gtja_statement.pdf"))


class GtjaPdfParserTest(unittest.TestCase):
    @unittest.skipUnless(SAMPLE.is_file(), "Set GTJA_SAMPLE_PDF to run the private statement fixture test.")
    def test_parse_rows(self):
        rows = parse_gtja_statement(SAMPLE)
        self.assertGreater(len(rows), 10)
        self.assertEqual(rows[0].settle_date, "20260112")
        self.assertEqual(rows[0].business, "证券卖出")
        self.assertEqual(rows[0].symbol, "518800")
        self.assertEqual(rows[2].activity_type, "bank_transfer_in")

    def test_transaction_fingerprint_ignores_source_file(self):
        left = ParsedGtjaRow(
            settle_date="20260112",
            account_code="48530",
            business="证券卖出",
            symbol="518800",
            name="黄金基金",
            price=9.657,
            quantity=600.0,
            gross_amount=5794.2,
            position_balance=1400.0,
            cash_amount=5793.91,
            cash_balance=6435.75,
            commission_total=0.29,
            commission_net=0.06,
            stamp_duty=0.0,
            transfer_fee=0.0,
            currency="CNY",
            other_fee=0.0,
            activity_type="security_sell",
            external_flow=False,
            asset_class="etf",
            market="CN",
            source_file="/tmp/a.pdf",
        )
        right = replace(left, source_file="/tmp/b.pdf")
        self.assertEqual(_row_fingerprint(left), _row_fingerprint(right))
        self.assertEqual(_transaction_id_for_row(left), _transaction_id_for_row(right))

    def test_special_business_mappings(self):
        self.assertEqual(_activity_type_for_business("历史数据迁移")[0], "history_migration")
        self.assertEqual(_activity_type_for_business("基金T+0转出")[0], "fund_redemption_in")
        self.assertEqual(_activity_type_for_business("基金非交易过户出")[0], "position_adjustment_out")
        self.assertEqual(_activity_type_for_business("现金红利")[0], "dividend")
        self.assertEqual(_activity_type_for_business("转增股")[0], "position_adjustment")
        self.assertEqual(_activity_type_for_business("并股调整")[0], "position_adjustment_out")
        self.assertEqual(_classify_asset_class("基金申购拨出", "850011", "850011"), "cash_management")
        self.assertEqual(_classify_asset_class("历史数据迁移", "132818", "海通报价"), "repo")


if __name__ == "__main__":
    unittest.main()
