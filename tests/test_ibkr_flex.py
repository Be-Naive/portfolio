import unittest

from portfolio_app.ibkr_client import IbkrClient, _instrument_bootstrap_prices, parse_flex_statement_xml


SAMPLE_XML = """
<FlexQueryResponse queryName="Daily Activity" type="AF">
  <FlexStatements count="1">
    <FlexStatement accountId="U1234567" fromDate="2026-03-01" toDate="2026-03-23">
      <AccountInformation accountId="U1234567" acctAlias="Main" currency="USD" fromDate="2026-03-01" toDate="2026-03-23" />
      <Trades>
        <Trade accountId="U1234567" assetCategory="STK" symbol="VOO" description="Vanguard S&amp;P 500 ETF" conid="756733" currency="USD" tradeDate="2026-03-10" settleDateTarget="2026-03-12" buySell="BUY" quantity="10" tradePrice="500" proceeds="-5000" ibCommission="-1" netCash="-5001" />
        <Trade accountId="U1234567" assetCategory="STK" symbol="AAPL" description="Apple Inc." conid="265598" currency="USD" tradeDate="2026-03-11" settleDateTarget="2026-03-13" buySell="SELL" quantity="5" tradePrice="210" proceeds="1050" ibCommission="-1" netCash="1049" />
        <Trade accountId="U1234567" assetCategory="CASH" symbol="USD.SGD" description="USD.SGD" conid="37928772" currency="SGD" tradeDate="2026-03-12" settleDateTarget="2026-03-12" buySell="BUY" quantity="100" tradePrice="1.28" proceeds="-128" ibCommission="0" netCash="0" exchange="IDEALFX" />
      </Trades>
      <CashTransactions>
        <CashTransaction accountId="U1234567" type="Deposits" description="Wire Deposit" currency="USD" reportDate="2026-03-09" amount="6000" />
        <CashTransaction accountId="U1234567" type="Dividends" description="Dividend Payment" currency="USD" reportDate="2026-03-20" amount="15" symbol="VOO" />
        <CashTransaction accountId="U1234567" type="Withholding Tax" description="Dividend Withholding Tax" currency="USD" reportDate="2026-03-20" amount="-3" symbol="VOO" />
      </CashTransactions>
      <CorporateActions>
        <CorporateAction accountId="U1234567" assetCategory="STK" symbol="IBKR" description="Interactive Brokers Group" actionDescription="IBKR SPLIT 4 FOR 1" conid="265598" currency="USD" reportDate="2026-03-21" type="FS" quantity="0.25" amount="0" />
        <CorporateAction accountId="U1234567" assetCategory="STK" symbol="QQQM" description="QQQM" actionDescription="QQQM CASH DIVIDEND - US TAX" conid="461386492" currency="USD" reportDate="2026-03-22" type="WH" quantity="0" amount="-1.11" />
      </CorporateActions>
      <OpenPositions>
        <OpenPosition accountId="U1234567" assetCategory="STK" symbol="VOO" description="Vanguard S&amp;P 500 ETF" conid="756733" currency="USD" position="10" markPrice="505" costBasisPrice="500" positionValue="5050" fifoPnlUnrealized="50" />
      </OpenPositions>
      <CashReport>
        <CashReportCurrency accountId="U1234567" currency="USD" endingCash="2064" />
      </CashReport>
      <ConversionRates>
        <ConversionRate reportDate="2026-03-21" fromCurrency="HKD" toCurrency="USD" rate="0.1287" />
      </ConversionRates>
    </FlexStatement>
  </FlexStatements>
</FlexQueryResponse>
""".strip()


class IbkrFlexParserTest(unittest.TestCase):
    def test_client_has_flex_request_helpers(self):
        client = IbkrClient(token="t", query_id="q")
        self.assertTrue(callable(getattr(client, "_send_request", None)))
        self.assertTrue(callable(getattr(client, "_poll_statement", None)))

    def test_parse_statement(self):
        parsed = parse_flex_statement_xml(SAMPLE_XML)
        self.assertEqual(parsed.query_name, "Daily Activity")
        self.assertEqual(parsed.statement_type, "AF")
        self.assertEqual(len(parsed.accounts), 1)
        self.assertEqual(len(parsed.instruments), 4)
        self.assertEqual(len(parsed.transactions), 8)
        self.assertEqual(len(parsed.positions), 1)
        self.assertEqual(len(parsed.cash_balances), 1)
        self.assertEqual(len(parsed.fx_rates), 1)
        self.assertTrue(any(tx["external_flow"] == 1 for tx in parsed.transactions))
        self.assertTrue(any(tx["activity_type"] == "stock_split" for tx in parsed.transactions))
        self.assertTrue(any(tx["activity_type"] == "fx_conversion" for tx in parsed.transactions))
        self.assertEqual(
            sum(1 for tx in parsed.transactions if tx["activity_type"] == "dividend_tax"),
            2,
        )
        self.assertEqual(parsed.positions[0]["market_value"], 5050.0)

    def test_option_bootstrap_price_prefers_close_price(self):
        prices = _instrument_bootstrap_prices(
            {
                "instrument_id": "ibkr:opt",
                "asset_class": "option",
                "price": 1.9,
                "currency": "USD",
                "transaction": {
                    "raw_json": {
                        "closePrice": "1.6582",
                    }
                },
            },
            "2026-05-08",
        )
        self.assertEqual(prices[0]["close_price"], 1.6582)


if __name__ == "__main__":
    unittest.main()
