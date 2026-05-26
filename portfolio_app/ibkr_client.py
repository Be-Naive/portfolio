from __future__ import annotations

import time
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, Iterable, List, Optional

import requests


FLEX_SERVICE_URL = "https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService"
RETRIABLE_ERROR_CODES = {"1001", "1003", "1004", "1005", "1006", "1007", "1008", "1009", "1018", "1019", "1021"}


class IbkrError(RuntimeError):
    pass


@dataclass
class IbkrSyncResult:
    service_url: str
    query_id: str
    statement_type: str
    accounts: int
    transactions: int
    positions: int
    cash_balances: int
    statement_from: str
    statement_to: str
    message: str
    snapshot_time: str


@dataclass
class FlexStatementData:
    query_name: str
    statement_type: str
    accounts: List[Dict[str, object]]
    instruments: List[Dict[str, object]]
    transactions: List[Dict[str, object]]
    positions: List[Dict[str, object]]
    cash_balances: List[Dict[str, object]]
    prices: List[Dict[str, object]]
    fx_rates: List[Dict[str, object]]
    statement_from: str
    statement_to: str
    snapshot_time: str


class IbkrClient:
    def __init__(
        self,
        token: str,
        query_id: str,
        service_url: str = FLEX_SERVICE_URL,
        timeout: int = 30,
        poll_interval_seconds: int = 5,
        max_poll_attempts: int = 12,
    ):
        self.token = token.strip()
        self.query_id = query_id.strip()
        self.service_url = service_url.rstrip("/")
        self.timeout = timeout
        self.poll_interval_seconds = poll_interval_seconds
        self.max_poll_attempts = max_poll_attempts
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "portfolio-prototype/0.1"})

    def sync(self, connection) -> IbkrSyncResult:
        reference_code = self._send_request()
        xml_text = self._poll_statement(reference_code)
        statement = parse_flex_statement_xml(xml_text)
        return import_flex_statement(
            connection,
            statement,
            service_url=self.service_url,
            query_id=self.query_id,
            source_label=f"IBKR Flex Query {statement.query_name or self.query_id}",
        )

    def _send_request(self) -> str:
        response = self.session.get(
            f"{self.service_url}/SendRequest",
            params={"t": self.token, "q": self.query_id, "v": 3},
            timeout=self.timeout,
        )
        root = _parse_xml_response(response.text, response.status_code)
        if root.tag != "FlexStatementResponse":
            raise IbkrError("Unexpected /SendRequest response from IBKR Flex Web Service.")
        status = root.findtext("Status", default="")
        if status != "Success":
            raise IbkrError(_format_flex_error(root))
        reference_code = root.findtext("ReferenceCode", default="").strip()
        if not reference_code:
            raise IbkrError("IBKR Flex Web Service did not return a reference code.")
        return reference_code

    def _poll_statement(self, reference_code: str) -> str:
        last_error = ""
        for _attempt in range(self.max_poll_attempts):
            response = self.session.get(
                f"{self.service_url}/GetStatement",
                params={"t": self.token, "q": reference_code, "v": 3},
                timeout=self.timeout,
            )
            root = _parse_xml_response(response.text, response.status_code)
            if root.tag == "FlexStatementResponse":
                status = root.findtext("Status", default="")
                if status == "Success" and root.find("FlexStatements") is not None:
                    return response.text
                error_code = root.findtext("ErrorCode", default="")
                if error_code in RETRIABLE_ERROR_CODES:
                    last_error = _format_flex_error(root)
                    time.sleep(self.poll_interval_seconds)
                    continue
                raise IbkrError(_format_flex_error(root))
            return response.text
        raise IbkrError(last_error or "IBKR Flex statement generation timed out.")


def import_flex_statement(
    connection,
    statement: FlexStatementData,
    *,
    service_url: str,
    query_id: str,
    source_label: str,
) -> IbkrSyncResult:
    from . import db

    for account in statement.accounts:
        db.upsert_account(connection, account)
    for instrument in statement.instruments:
        db.upsert_instrument(connection, instrument)
    for price in statement.prices:
        db.upsert_price(connection, price)
    for fx_rate in statement.fx_rates:
        db.upsert_fx_rate(connection, fx_rate)

    for transaction in statement.transactions:
        db.insert_transaction(connection, transaction)
        if transaction["external_flow"]:
            amount = transaction.get("cash_amount") or 0.0
            db.insert_cash_flow(
                connection,
                {
                    "id": f"cf:{transaction['id']}",
                    "transaction_id": transaction["id"],
                    "account_id": transaction["account_id"],
                    "flow_date": transaction["settle_date"],
                    "direction": "in" if amount >= 0 else "out",
                    "amount": abs(amount),
                    "currency": transaction["currency"],
                    "description": transaction["description"],
                },
            )

    if statement.positions:
        db.replace_position_snapshots(connection, statement.snapshot_time, "ibkr_flex", statement.positions)
    if statement.cash_balances:
        db.replace_cash_balances(connection, statement.snapshot_time, "ibkr_flex", statement.cash_balances)

    return IbkrSyncResult(
        service_url=service_url,
        query_id=query_id,
        statement_type=statement.statement_type,
        accounts=len(statement.accounts),
        transactions=len(statement.transactions),
        positions=len(statement.positions),
        cash_balances=len(statement.cash_balances),
        statement_from=statement.statement_from,
        statement_to=statement.statement_to,
        message=f"Imported {source_label}",
        snapshot_time=statement.snapshot_time,
    )


def import_flex_statement_file(connection, xml_text: str, source_label: str = "IBKR Flex XML file") -> IbkrSyncResult:
    statement = parse_flex_statement_xml(xml_text)
    return import_flex_statement(
        connection,
        statement,
        service_url="local-file",
        query_id=source_label,
        source_label=source_label,
    )


def parse_flex_statement_xml(xml_text: str) -> FlexStatementData:
    root = ET.fromstring(xml_text)
    if root.tag == "FlexStatementResponse":
        raise IbkrError(_format_flex_error(root))

    statement_type = root.attrib.get("type", "")
    query_name = root.attrib.get("queryName", "")
    flex_statements = root.findall(".//FlexStatement")
    if not flex_statements:
        raise IbkrError("No FlexStatement node was found in the IBKR response.")

    accounts: List[Dict[str, object]] = []
    instruments: Dict[str, Dict[str, object]] = {}
    transactions: List[Dict[str, object]] = []
    positions: List[Dict[str, object]] = []
    cash_balances: List[Dict[str, object]] = []
    prices: List[Dict[str, object]] = []
    fx_rates: List[Dict[str, object]] = []
    seen_instruments = set()
    known_instrument_ids_by_symbol: Dict[str, str] = {}
    statement_from_candidates: List[str] = []
    statement_to_candidates: List[str] = []

    for flex_statement in flex_statements:
        account_info = flex_statement.find("AccountInformation")
        default_account_id = (
            _attr(account_info, "accountId")
            or _attr(account_info, "accountID")
            or _attr(account_info, "acctId")
            or "unknown"
        )
        default_currency = _normalize_currency(_attr(account_info, "currency") or "USD")
        current_from = _normalize_date(_attr(account_info, "fromDate") or _attr(flex_statement, "fromDate") or "")
        current_to = _normalize_date(_attr(account_info, "toDate") or _attr(flex_statement, "toDate") or "")
        statement_from_candidates.append(current_from)
        statement_to_candidates.append(current_to)

        accounts.append(
            {
                "id": f"ibkr:{default_account_id}",
                "broker": "ibkr",
                "account_code": default_account_id,
                "display_name": _attr(account_info, "acctAlias") or _attr(account_info, "accountAlias") or default_account_id,
                "base_currency": default_currency,
                "metadata_json": _node_attributes(account_info),
            }
        )

        for trade in flex_statement.findall(".//Trades/*"):
            payload = _parse_trade(trade, default_account_id, default_currency)
            if not payload:
                continue
            instruments[payload["instrument"]["id"]] = payload["instrument"]
            known_instrument_ids_by_symbol[payload["instrument"]["symbol"]] = payload["instrument"]["id"]
            instrument_key = (payload["instrument_id"], payload["instrument_name"], payload["asset_class"], payload["market"], payload["currency"])
            if instrument_key not in seen_instruments:
                prices.extend(_instrument_bootstrap_prices(payload, current_to))
                seen_instruments.add(instrument_key)
            transactions.append(payload["transaction"])

        cash_nodes = list(flex_statement.findall(".//CashTransactions/*"))
        detail_cash_signatures = {
            _cash_transaction_signature(node)
            for node in cash_nodes
            if (_attr(node, "levelOfDetail") or "").upper() == "DETAIL"
        }
        for cash_tx in cash_nodes:
            if _should_skip_summary_cash_transaction(cash_tx, detail_cash_signatures):
                continue
            payload = _parse_cash_transaction(cash_tx, default_account_id, default_currency, known_instrument_ids_by_symbol)
            if payload:
                if payload.get("instrument"):
                    instruments[payload["instrument"]["id"]] = payload["instrument"]
                    known_instrument_ids_by_symbol[payload["instrument"]["symbol"]] = payload["instrument"]["id"]
                transactions.append(payload["transaction"])

        for corp_action in flex_statement.findall(".//CorporateActions/*"):
            payload = _parse_corporate_action(corp_action, default_account_id, default_currency, known_instrument_ids_by_symbol)
            if payload:
                if payload.get("instrument"):
                    instruments[payload["instrument"]["id"]] = payload["instrument"]
                    known_instrument_ids_by_symbol[payload["instrument"]["symbol"]] = payload["instrument"]["id"]
                transactions.append(payload["transaction"])

        for section_name in ("OpenPositions", "OpenPositionSummary"):
            section = flex_statement.find(section_name)
            if section is None:
                continue
            for node in list(section):
                payload = _parse_open_position(node, default_account_id, default_currency, current_to)
                if not payload:
                    continue
                instruments[payload["instrument"]["id"]] = payload["instrument"]
                known_instrument_ids_by_symbol[payload["instrument"]["symbol"]] = payload["instrument"]["id"]
                positions.append(payload["position"])
                prices.append(payload["price"])

        found_cash_report = False
        for section_name in ("CashReport", "CashReportSummary"):
            section = flex_statement.find(section_name)
            if section is None:
                continue
            found_cash_report = True
            for node in list(section):
                payload = _parse_cash_balance(node, default_account_id, default_currency)
                if payload:
                    cash_balances.append(payload)

        if not found_cash_report and account_info is not None:
            ending_cash = _floatish(_attr(account_info, "endingCash"))
            if ending_cash:
                cash_balances.append(
                    {
                        "account_id": f"ibkr:{default_account_id}",
                        "currency": default_currency,
                        "amount": ending_cash,
                        "raw_json": _node_attributes(account_info),
                    }
                )

        conversion_rates = flex_statement.find("ConversionRates")
        if conversion_rates is not None:
            for node in list(conversion_rates):
                payload = _parse_conversion_rate(node)
                if payload:
                    fx_rates.append(payload)

    transactions.sort(key=lambda row: (row["trade_date"], row["settle_date"], row["id"]))
    statement_from = min(filter(None, statement_from_candidates), default="")
    statement_to = max(filter(None, statement_to_candidates), default="")
    snapshot_time = _snapshot_time(statement_to or statement_from)
    return FlexStatementData(
        query_name=query_name,
        statement_type=statement_type,
        accounts=accounts,
        instruments=list(instruments.values()),
        transactions=transactions,
        positions=positions,
        cash_balances=cash_balances,
        prices=prices,
        fx_rates=fx_rates,
        statement_from=statement_from,
        statement_to=statement_to,
        snapshot_time=snapshot_time,
    )


def _parse_trade(node: ET.Element, account_id: str, default_currency: str):
    symbol = _attr(node, "symbol") or _attr(node, "underlyingSymbol") or _attr(node, "description")
    if not symbol:
        return None
    quantity = abs(_floatish(_attr(node, "quantity") or _attr(node, "tradeQuantity")))
    buy_sell = (_attr(node, "buySell") or _attr(node, "transactionType") or "").upper()
    signed_quantity = quantity if "BUY" in buy_sell else -quantity
    price = _floatish(_attr(node, "tradePrice"))
    proceeds = _floatish(_attr(node, "proceeds"))
    commission = abs(_floatish(_attr(node, "ibCommission")))
    net_cash = _floatish(_attr(node, "netCash"))
    currency = _normalize_currency(_attr(node, "currency") or default_currency)
    settle_date = _normalize_date(_attr(node, "settleDateTarget") or _attr(node, "settleDate") or _attr(node, "dateTime") or _attr(node, "tradeDate"))
    trade_date = _normalize_date(_attr(node, "tradeDate") or _attr(node, "dateTime") or settle_date)
    description = _attr(node, "description") or symbol
    asset_class = _map_asset_class(_attr(node, "assetCategory"))
    market = _infer_market(currency, symbol, _attr(node, "listingExchange") or _attr(node, "exchange"))
    conid = _attr(node, "conid") or symbol
    instrument_id = f"ibkr:{conid}"
    is_fx_conversion = asset_class == "cash" and market == "IDEALFX" and "." in symbol
    if is_fx_conversion:
        activity_type = "fx_conversion"
        asset_class = "fx_pair"
    else:
        activity_type = "security_buy" if signed_quantity >= 0 else "security_sell"
    transaction_id = f"ibkr:flex:trade:{uuid.uuid5(uuid.NAMESPACE_URL, str(sorted(node.attrib.items())))}"

    transaction = {
        "id": transaction_id,
        "broker": "ibkr",
        "account_id": f"ibkr:{account_id}",
        "instrument_id": instrument_id,
        "settle_date": settle_date,
        "trade_date": trade_date,
        "activity_type": activity_type,
        "description": description,
        "external_flow": 0,
        "quantity": quantity,
        "price": price or None,
        "gross_amount": abs(proceeds) or None,
        "cash_amount": net_cash or proceeds or None,
        "position_balance": None,
        "cash_balance": None,
        "commission_total": commission or None,
        "commission_net": commission or None,
        "stamp_duty": None,
        "transfer_fee": None,
        "other_fee": None,
        "currency": currency,
        "source_file": f"IBKR Flex Query {account_id}",
        "raw_json": _node_attributes(node),
    }
    return {
        "instrument_id": instrument_id,
        "instrument_symbol": symbol,
        "instrument_name": description,
        "asset_class": asset_class,
        "market": market,
        "currency": currency,
        "price": price,
        "instrument": {
            "id": instrument_id,
            "broker": "ibkr",
            "symbol": symbol,
            "name": description,
            "asset_class": asset_class,
            "market": market,
            "currency": currency,
            "metadata_json": _node_attributes(node),
        },
        "transaction": transaction,
    }


def _parse_cash_transaction(
    node: ET.Element,
    account_id: str,
    default_currency: str,
    known_instrument_ids_by_symbol: Dict[str, str],
):
    amount = _floatish(_attr(node, "amount"))
    if amount == 0:
        return None
    currency = _normalize_currency(_attr(node, "currency") or default_currency)
    description = _attr(node, "description") or _attr(node, "type") or "Cash Transaction"
    category = _classify_cash_activity(node)
    settle_date = _normalize_date(_attr(node, "settleDate") or _attr(node, "reportDate") or _attr(node, "dateTime") or _attr(node, "date"))
    trade_date = _normalize_date(_attr(node, "dateTime") or _attr(node, "reportDate") or settle_date)
    symbol = _attr(node, "symbol")
    conid = _attr(node, "conid")
    instrument_id = None
    if conid:
        instrument_id = f"ibkr:{conid}"
    elif symbol:
        instrument_id = known_instrument_ids_by_symbol.get(symbol, f"ibkr:{symbol}")
    transaction_id = f"ibkr:flex:cash:{uuid.uuid5(uuid.NAMESPACE_URL, str(sorted(node.attrib.items())))}"
    instrument = None
    if instrument_id:
        instrument = {
            "id": instrument_id,
            "broker": "ibkr",
            "symbol": symbol or _attr(node, "conid"),
            "name": symbol or description,
            "asset_class": "security",
            "market": _infer_market(currency, symbol or "", _attr(node, "listingExchange") or _attr(node, "exchange")),
            "currency": currency,
            "metadata_json": _node_attributes(node),
        }
    return {
        "instrument": instrument,
        "transaction": {
            "id": transaction_id,
            "broker": "ibkr",
            "account_id": f"ibkr:{account_id}",
            "instrument_id": instrument_id,
            "settle_date": settle_date,
            "trade_date": trade_date,
            "activity_type": category["activity_type"],
            "description": description,
            "external_flow": int(category["external_flow"]),
            "quantity": None,
            "price": None,
            "gross_amount": abs(amount),
            "cash_amount": amount,
            "position_balance": None,
            "cash_balance": None,
            "commission_total": None,
            "commission_net": None,
            "stamp_duty": None,
            "transfer_fee": None,
            "other_fee": None,
            "currency": currency,
            "source_file": f"IBKR Flex Query {account_id}",
            "raw_json": _node_attributes(node),
        },
    }


def _parse_open_position(node: ET.Element, account_id: str, default_currency: str, statement_to: str):
    symbol = _attr(node, "symbol") or _attr(node, "description")
    if not symbol:
        return None
    conid = _attr(node, "conid") or symbol
    instrument_id = f"ibkr:{conid}"
    currency = _normalize_currency(_attr(node, "currency") or default_currency)
    quantity = _floatish(_attr(node, "position"))
    if quantity == 0:
        return None
    mark_price = _floatish(_attr(node, "markPrice") or _attr(node, "price"))
    average_cost = _floatish(_attr(node, "costBasisPrice") or _attr(node, "averageCost"))
    market_value = _floatish(_attr(node, "positionValue") or _attr(node, "marketValue") or _attr(node, "value"))
    cost_basis_money = _floatish(_attr(node, "costBasisMoney"))
    unrealized = _floatish(_attr(node, "fifoPnlUnrealized") or _attr(node, "mtmPnl") or _attr(node, "unrealizedProfitAndLoss"))
    if unrealized == 0 and market_value and cost_basis_money:
        unrealized = market_value - cost_basis_money
    market = _infer_market(currency, symbol, _attr(node, "listingExchange") or _attr(node, "exchange"))
    asset_class = _map_asset_class(_attr(node, "assetCategory"))
    snapshot = {
        "account_id": f"ibkr:{account_id}",
        "instrument_id": instrument_id,
        "quantity": quantity,
        "average_cost": average_cost or None,
        "market_price": mark_price or None,
        "market_value": market_value or None,
        "unrealized_pnl": unrealized or None,
        "currency": currency,
        "raw_json": _node_attributes(node),
    }
    price = {
        "instrument_id": instrument_id,
        "price_date": statement_to or datetime.utcnow().date().isoformat(),
        "close_price": mark_price or average_cost or 0.0,
        "currency": currency,
        "source": "ibkr_flex_position",
    }
    return {
        "position": snapshot,
        "price": price,
        "asset_class": asset_class,
        "market": market,
        "instrument": {
            "id": instrument_id,
            "broker": "ibkr",
            "symbol": symbol,
            "name": _attr(node, "description") or symbol,
            "asset_class": asset_class,
            "market": market,
            "currency": currency,
            "metadata_json": _node_attributes(node),
        },
    }


def _parse_corporate_action(
    node: ET.Element,
    account_id: str,
    default_currency: str,
    known_instrument_ids_by_symbol: Dict[str, str],
):
    description = _attr(node, "actionDescription") or _attr(node, "description") or _attr(node, "type") or "Corporate Action"
    currency = _normalize_currency(_attr(node, "currency") or default_currency)
    symbol = _attr(node, "symbol")
    conid = _attr(node, "conid")
    instrument_id = None
    if conid:
        instrument_id = f"ibkr:{conid}"
    elif symbol:
        instrument_id = known_instrument_ids_by_symbol.get(symbol, f"ibkr:{symbol}")

    quantity = _floatish(_attr(node, "quantity"))
    amount = _floatish(_attr(node, "amount") or _attr(node, "proceeds") or _attr(node, "value"))
    report_date = _normalize_date(_attr(node, "reportDate") or _attr(node, "dateTime"))
    activity_type = _classify_corporate_action(node, description, amount, quantity)
    transaction_id = f"ibkr:flex:corp:{uuid.uuid5(uuid.NAMESPACE_URL, str(sorted(node.attrib.items())))}"

    instrument = None
    if instrument_id:
        instrument = {
            "id": instrument_id,
            "broker": "ibkr",
            "symbol": symbol or conid,
            "name": _attr(node, "description") or symbol or description,
            "asset_class": _map_asset_class(_attr(node, "assetCategory")),
            "market": _infer_market(currency, symbol or "", _attr(node, "listingExchange") or _attr(node, "exchange")),
            "currency": currency,
            "metadata_json": _node_attributes(node),
        }

    transaction = {
        "id": transaction_id,
        "broker": "ibkr",
        "account_id": f"ibkr:{account_id}",
        "instrument_id": instrument_id,
        "settle_date": report_date,
        "trade_date": report_date,
        "activity_type": activity_type,
        "description": description,
        "external_flow": 0,
        "quantity": quantity or None,
        "price": None,
        "gross_amount": abs(amount) or None,
        "cash_amount": None if activity_type in {"stock_split", "position_adjustment"} else (amount or None),
        "position_balance": None,
        "cash_balance": None,
        "commission_total": None,
        "commission_net": None,
        "stamp_duty": None,
        "transfer_fee": None,
        "other_fee": None,
        "currency": currency,
        "source_file": f"IBKR Flex Query {account_id}",
        "raw_json": _node_attributes(node),
    }
    return {"instrument": instrument, "transaction": transaction}


def _parse_cash_balance(node: ET.Element, account_id: str, default_currency: str):
    currency = _normalize_currency(_attr(node, "currency") or default_currency)
    amount = (
        _floatish(_attr(node, "endingCash"))
        or _floatish(_attr(node, "endingSettledCash"))
        or _floatish(_attr(node, "totalCashValue"))
        or _floatish(_attr(node, "cash"))
    )
    if amount == 0 and not _attr(node, "currency"):
        return None
    return {
        "account_id": f"ibkr:{account_id}",
        "currency": currency,
        "amount": amount,
        "raw_json": _node_attributes(node),
    }


def _parse_conversion_rate(node: ET.Element):
    from_currency = _normalize_currency(_attr(node, "fromCurrency"))
    to_currency = _normalize_currency(_attr(node, "toCurrency"))
    report_date = _normalize_date(_attr(node, "reportDate"))
    rate = _floatish(_attr(node, "rate"))
    if not from_currency or not to_currency or not report_date or rate == 0:
        return None
    return {
        "rate_date": report_date,
        "base_currency": from_currency,
        "quote_currency": to_currency,
        "rate": rate,
        "source": "ibkr_flex_conversion",
    }


def _instrument_bootstrap_prices(payload: Dict[str, object], statement_to: str) -> List[Dict[str, object]]:
    raw = payload.get("raw_json") or {}
    if not raw and payload.get("transaction"):
        raw = payload["transaction"].get("raw_json") or {}
    price = payload.get("price")
    if payload.get("asset_class") == "option":
        option_close = _floatish(raw.get("closePrice")) if isinstance(raw, dict) else 0.0
        if option_close > 0:
            price = option_close
    if not price:
        return []
    return [
        {
            "instrument_id": payload["instrument_id"],
            "price_date": statement_to or datetime.utcnow().date().isoformat(),
            "close_price": price,
            "currency": payload["currency"],
            "source": "ibkr_flex_trade",
        }
    ]


def _classify_cash_activity(node: ET.Element) -> Dict[str, object]:
    text = " ".join(
        filter(
            None,
            [
                (_attr(node, "type") or ""),
                (_attr(node, "description") or ""),
            ],
        )
    ).lower()
    if any(keyword in text for keyword in ("deposit", "contribution", "wire in", "ach credit", "incoming")):
        return {"activity_type": "bank_transfer_in", "external_flow": True}
    if any(keyword in text for keyword in ("withdraw", "wire out", "ach debit", "outgoing")):
        return {"activity_type": "bank_transfer_out", "external_flow": True}
    if "withholding" in text or "tax" in text:
        return {"activity_type": "dividend_tax", "external_flow": False}
    if "dividend" in text:
        return {"activity_type": "dividend", "external_flow": False}
    if "interest" in text:
        return {"activity_type": "interest_credit", "external_flow": False}
    if "fee" in text or "commission" in text:
        return {"activity_type": "fee", "external_flow": False}
    return {"activity_type": "cash_adjustment", "external_flow": False}


def _classify_corporate_action(node: ET.Element, description: str, amount: float, quantity: float) -> str:
    upper = " ".join(
        filter(
            None,
            [
                (_attr(node, "type") or ""),
                (_attr(node, "code") or ""),
                description,
            ],
        )
    ).upper()
    if "TAX" in upper or "WITHHOLD" in upper:
        return "dividend_tax"
    if "DIV" in upper or "DIVIDEND" in upper:
        return "dividend"
    if "SPLIT" in upper:
        return "stock_split"
    if abs(quantity) > 0 and abs(amount) == 0:
        return "position_adjustment"
    if abs(amount) > 0:
        return "corporate_action_cash"
    return "corporate_action"


def _cash_transaction_signature(node: ET.Element):
    return (
        _normalize_date(_attr(node, "dateTime") or _attr(node, "reportDate") or _attr(node, "settleDate")),
        _normalize_date(_attr(node, "settleDate") or _attr(node, "reportDate")),
        _normalize_currency(_attr(node, "currency")),
        _floatish(_attr(node, "amount")),
        _attr(node, "type"),
        _attr(node, "description"),
    )


def _should_skip_summary_cash_transaction(node: ET.Element, detail_signatures) -> bool:
    return (
        (_attr(node, "levelOfDetail") or "").upper() == "SUMMARY"
        and _cash_transaction_signature(node) in detail_signatures
    )


def _map_asset_class(raw: str) -> str:
    normalized = (raw or "").upper()
    mapping = {
        "STK": "equity",
        "ETF": "etf",
        "OPT": "option",
        "FOP": "option",
        "FUT": "future",
        "BOND": "bond",
        "CASH": "cash",
        "FUND": "fund",
        "WAR": "warrant",
    }
    return mapping.get(normalized, normalized.lower() or "security")


def _normalize_currency(value: str) -> str:
    return (value or "").upper() or "USD"


def _infer_market(currency: str, symbol: str, exchange: str) -> str:
    exchange = (exchange or "").upper()
    if exchange:
        return exchange
    if currency == "USD":
        return "US"
    if currency == "HKD":
        return "HK"
    if currency in {"CNH", "CNY"}:
        return "CN"
    if symbol.endswith(".HK"):
        return "HK"
    return currency or "UNKNOWN"


def _normalize_date(raw: str) -> str:
    raw = (raw or "").strip()
    if not raw:
        return datetime.utcnow().date().isoformat()
    for pattern in ("%Y-%m-%d", "%Y%m%d", "%Y%m%d;%H%M%S", "%Y-%m-%d;%H%M%S", "%Y-%m-%d %H:%M:%S", "%Y%m%d %H:%M:%S"):
        try:
            return datetime.strptime(raw, pattern).date().isoformat()
        except ValueError:
            continue
    if ";" in raw:
        return _normalize_date(raw.split(";", 1)[0])
    if " " in raw:
        return _normalize_date(raw.split(" ", 1)[0])
    return raw


def _snapshot_time(statement_date: str) -> str:
    statement_day = _normalize_date(statement_date or datetime.utcnow().date().isoformat())
    return f"{statement_day}T23:59:59Z"


def _node_attributes(node: Optional[ET.Element]) -> Dict[str, object]:
    return dict(node.attrib) if node is not None else {}


def _attr(node: Optional[ET.Element], key: str) -> str:
    if node is None:
        return ""
    return node.attrib.get(key, "").strip()


def _floatish(value) -> float:
    if value in (None, "", "--"):
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "")
    if text.startswith("(") and text.endswith(")"):
        text = f"-{text[1:-1]}"
    try:
        return float(text)
    except ValueError:
        return 0.0


def _parse_xml_response(xml_text: str, status_code: int) -> ET.Element:
    if status_code >= 400:
        raise IbkrError(f"IBKR Flex request failed: HTTP {status_code}")
    try:
        return ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise IbkrError("IBKR Flex response was not valid XML.") from exc


def _format_flex_error(root: ET.Element) -> str:
    error_code = root.findtext("ErrorCode", default="").strip()
    error_message = root.findtext("ErrorMessage", default="Unknown Flex Web Service error.").strip()
    if error_code:
        return f"IBKR Flex error {error_code}: {error_message}"
    return error_message
