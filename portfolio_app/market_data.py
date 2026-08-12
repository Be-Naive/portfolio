from __future__ import annotations

import csv
import json
from datetime import date, datetime, timedelta
from pathlib import Path
import re
from typing import Iterable
import xml.etree.ElementTree as ET

import requests


ECB_HISTORY_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-hist.xml"
YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
EASTMONEY_FUND_NAV_URL = "https://api.fund.eastmoney.com/f10/lsjz"
SINA_KLINE_URL = "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData"


def refresh_market_data(connection, instrument_ids: Iterable[str] | None = None, include_fx: bool = True) -> dict:
    from . import db

    session = requests.Session()
    session.headers.update({"User-Agent": "portfolio-prototype/0.1"})
    price_count = 0
    fx_count = 0
    selected_ids = sorted({instrument_id for instrument_id in (instrument_ids or []) if instrument_id})
    target_mode = "targeted" if selected_ids else "full"
    request_timeout = 8 if selected_ids else 30
    price_ranges = _price_sync_ranges(connection, selected_ids)
    transaction_starts = _instrument_transaction_start_dates(connection, selected_ids)

    if include_fx:
        for payload in _fetch_ecb_history(session):
            db.upsert_fx_rate(connection, payload)
            fx_count += 1

    instruments = db.fetch_all(connection, *_instrument_query(selected_ids, """
        SELECT DISTINCT id, broker, symbol, asset_class, market, currency
        FROM instruments
        WHERE asset_class IN ('equity', 'etf', 'security')
        ORDER BY broker, symbol
        """, id_column="id"))
    for instrument in instruments:
        yahoo_symbol = _yahoo_symbol_for_instrument(dict(instrument))
        if not yahoo_symbol:
            continue
        try:
            start_date = _resolve_incremental_start(
                price_ranges.get((instrument["id"], "yahoo_chart_close")),
                price_ranges.get((instrument["id"], "yahoo_chart_adjusted")),
                transaction_start=transaction_starts.get(instrument["id"]),
                fallback_days=3650 if not selected_ids else 365,
                padding_days=10,
            )
            for payload in _fetch_yahoo_history(
                session,
                dict(instrument),
                yahoo_symbol,
                start_date=start_date,
                request_timeout=request_timeout,
            ):
                db.upsert_price(connection, payload)
                price_count += 1
        except requests.RequestException:
            continue
        except ValueError:
            continue

    cn_market_instruments = db.fetch_all(connection, *_instrument_query(selected_ids, """
        SELECT DISTINCT id, broker, symbol, asset_class, market, currency
        FROM instruments
        WHERE broker = 'gtja'
          AND market = 'CN'
          AND (
            asset_class IN ('equity', 'etf', 'bond_fund')
            OR symbol GLOB '5*'
          )
        ORDER BY symbol
        """, id_column="id"))
    for instrument in cn_market_instruments:
        sina_symbol = _sina_symbol_for_instrument(dict(instrument))
        if not sina_symbol:
            continue
        try:
            start_date = _resolve_incremental_start(
                price_ranges.get((instrument["id"], "sina_kline")),
                transaction_start=transaction_starts.get(instrument["id"]),
                fallback_days=3650 if not selected_ids else 365,
                padding_days=10,
            )
            for payload in _fetch_sina_history(
                session,
                dict(instrument),
                sina_symbol,
                start_date=start_date,
                request_timeout=request_timeout,
            ):
                db.upsert_price(connection, payload)
                price_count += 1
        except requests.RequestException:
            continue
        except ValueError:
            continue

    fund_instruments = db.fetch_all(connection, *_instrument_query(selected_ids, """
        SELECT
            i.id,
            i.broker,
            i.symbol,
            i.asset_class,
            i.market,
            i.currency,
            MAX(CASE WHEN t.activity_type = 'fund_subscription_confirm' THEN 1 ELSE 0 END) AS has_confirm
        FROM instruments i
        LEFT JOIN transactions t ON t.instrument_id = i.id
        WHERE i.broker = 'gtja'
          AND i.market = 'CN'
          AND i.asset_class = 'fund'
        GROUP BY i.id, i.broker, i.symbol, i.asset_class, i.market, i.currency
        ORDER BY i.symbol
        """, id_column="i.id"))
    for instrument in fund_instruments:
        fund_code = _eastmoney_fund_code_for_instrument(dict(instrument))
        if not fund_code:
            continue
        try:
            start_date = _resolve_incremental_start(
                price_ranges.get((instrument["id"], "eastmoney_fund_nav")),
                transaction_start=transaction_starts.get(instrument["id"]),
                fallback_days=3650 if not selected_ids else 365,
                padding_days=10,
            )
            for payload in _fetch_eastmoney_fund_history(
                session,
                dict(instrument),
                fund_code,
                start_date=start_date.isoformat(),
                request_timeout=request_timeout,
            ):
                db.upsert_price(connection, payload)
                price_count += 1
        except requests.RequestException:
            continue
        except ValueError:
            continue

    return {
        "mode": target_mode,
        "prices": price_count,
        "fx_rates": fx_count,
        "instrument_count": len(selected_ids) if selected_ids else len(instruments),
        "instrument_ids": selected_ids,
    }


def refresh_fx_rates(connection) -> dict:
    from . import db

    session = requests.Session()
    session.headers.update({"User-Agent": "portfolio-prototype/0.1"})
    fx_count = 0

    for payload in _fetch_ecb_history(session):
        db.upsert_fx_rate(connection, payload)
        fx_count += 1

    return {"fx_rates": fx_count}


def _instrument_query(instrument_ids: list[str], base_query: str, id_column: str = "id") -> tuple[str, list[str]]:
    if not instrument_ids:
        return base_query, []
    match = re.search(r"\bORDER BY\b", base_query, flags=re.IGNORECASE)
    filter_clause = f" AND {id_column} IN ({', '.join('?' for _ in instrument_ids)})"
    if match:
        query = f"{base_query[:match.start()]}{filter_clause}\n{base_query[match.start():]}"
    else:
        query = f"{base_query}\n{filter_clause}"
    return query, list(instrument_ids)


def _price_sync_ranges(connection, instrument_ids: list[str]) -> dict[tuple[str, str], tuple[date, date]]:
    from . import db

    query = """
        SELECT instrument_id, source, MIN(price_date) AS min_price_date, MAX(price_date) AS max_price_date
        FROM price_history
    """
    params: list[str] = []
    if instrument_ids:
        query += f" WHERE instrument_id IN ({', '.join('?' for _ in instrument_ids)})"
        params.extend(instrument_ids)
    query += " GROUP BY instrument_id, source"
    rows = db.fetch_all(connection, query, params)
    result: dict[tuple[str, str], tuple[date, date]] = {}
    for row in rows:
        if row["min_price_date"] and row["max_price_date"]:
            result[(row["instrument_id"], row["source"])] = (
                date.fromisoformat(row["min_price_date"]),
                date.fromisoformat(row["max_price_date"]),
            )
    return result


def _instrument_transaction_start_dates(connection, instrument_ids: list[str]) -> dict[str, date]:
    from . import db

    query = "SELECT instrument_id, MIN(trade_date) AS start_date FROM transactions WHERE instrument_id IS NOT NULL"
    params: list[str] = []
    if instrument_ids:
        query += f" AND instrument_id IN ({', '.join('?' for _ in instrument_ids)})"
        params.extend(instrument_ids)
    query += " GROUP BY instrument_id"
    rows = db.fetch_all(connection, query, params)
    return {
        row["instrument_id"]: date.fromisoformat(row["start_date"])
        for row in rows
        if row["start_date"]
    }


def _resolve_incremental_start(
    *price_ranges: tuple[date, date] | None,
    transaction_start: date | None = None,
    fallback_days: int,
    padding_days: int,
) -> date:
    known_ranges = [item for item in price_ranges if isinstance(item, tuple)]

    if transaction_start and known_ranges:
        earliest_price = min(item[0] for item in known_ranges)
        latest_price = max(item[1] for item in known_ranges)
        if earliest_price > transaction_start + timedelta(days=padding_days):
            return transaction_start - timedelta(days=padding_days)
        return latest_price - timedelta(days=padding_days)

    if known_ranges:
        latest_price = max(item[1] for item in known_ranges)
        return latest_price - timedelta(days=padding_days)

    if transaction_start:
        return transaction_start - timedelta(days=padding_days)

    return datetime.now().date() - timedelta(days=fallback_days)


def import_price_csv(connection, csv_path: Path) -> dict:
    from . import db

    rows_imported = 0
    instruments = db.fetch_all(connection, "SELECT id, broker, symbol FROM instruments")
    by_key = {
        ((row["broker"] or "").lower(), (row["symbol"] or "").upper()): row["id"]
        for row in instruments
    }
    by_symbol = {}
    for row in instruments:
        by_symbol.setdefault((row["symbol"] or "").upper(), row["id"])

    with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        for raw_row in reader:
            symbol = (raw_row.get("symbol") or raw_row.get("ticker") or "").strip().upper()
            broker = (raw_row.get("broker") or "").strip().lower()
            instrument_id = (raw_row.get("instrument_id") or "").strip()
            if not instrument_id:
                instrument_id = by_key.get((broker, symbol)) if broker else by_symbol.get(symbol)
            if not instrument_id:
                continue
            price_date = (raw_row.get("date") or raw_row.get("price_date") or "").strip()
            close_price = float(raw_row.get("close") or raw_row.get("close_price") or 0.0)
            currency = (raw_row.get("currency") or "").strip().upper()
            if not price_date or not close_price:
                continue
            if not currency:
                instrument_row = db.fetch_one(connection, "SELECT currency FROM instruments WHERE id = ?", [instrument_id])
                currency = instrument_row["currency"] if instrument_row else "USD"
            db.upsert_price(
                connection,
                {
                    "instrument_id": instrument_id,
                    "price_date": price_date,
                    "close_price": close_price,
                    "currency": currency,
                    "source": "manual_csv",
                },
            )
            rows_imported += 1
    return {"prices": rows_imported}


def _fetch_ecb_history(session: requests.Session) -> list[dict]:
    response = session.get(ECB_HISTORY_URL, timeout=30)
    response.raise_for_status()
    root = ET.fromstring(response.text)
    namespace = {"gesmes": "http://www.gesmes.org/xml/2002-08-01", "def": "http://www.ecb.int/vocabulary/2002-08-01/eurofxref"}

    payloads = []
    for day_node in root.findall(".//def:Cube[@time]", namespace):
        rate_date = day_node.attrib["time"]
        for rate_node in day_node.findall("def:Cube[@currency]", namespace):
            currency = (rate_node.attrib.get("currency") or "").upper()
            rate = float(rate_node.attrib.get("rate") or 0.0)
            if not currency or not rate:
                continue
            payloads.append(
                {
                    "rate_date": rate_date,
                    "base_currency": "EUR",
                    "quote_currency": currency,
                    "rate": rate,
                    "source": "ecb_reference",
                }
            )
    return payloads


def _fetch_yahoo_history(
    session: requests.Session,
    instrument: dict,
    yahoo_symbol: str,
    start_date: date | None = None,
    request_timeout: int = 30,
) -> list[dict]:
    if start_date is None:
        start_date = datetime.now().date() - timedelta(days=3650)
    response = session.get(
        YAHOO_CHART_URL.format(symbol=yahoo_symbol),
        params={
            "interval": "1d",
            "period1": int(datetime.combine(start_date, datetime.min.time()).timestamp()),
            "period2": int(datetime.now().timestamp()),
            "includeAdjustedClose": "true",
            "events": "div,splits",
        },
        timeout=request_timeout,
    )
    response.raise_for_status()
    payload = response.json()
    result = (payload.get("chart") or {}).get("result") or []
    if not result:
        return []
    series = result[0]
    timestamps = series.get("timestamp") or []
    adjusted_series = (((series.get("indicators") or {}).get("adjclose") or []) or [{}])[0].get("adjclose") or []
    raw_close_series = (((series.get("indicators") or {}).get("quote") or []) or [{}])[0].get("close") or []

    rows = []
    for index, timestamp in enumerate(timestamps):
        price_date = datetime.utcfromtimestamp(timestamp).date().isoformat()
        raw_close = raw_close_series[index] if index < len(raw_close_series) else None
        adjusted_close = adjusted_series[index] if index < len(adjusted_series) else None
        if raw_close not in (None, 0):
            rows.append(
                {
                    "instrument_id": instrument["id"],
                    "price_date": price_date,
                    "close_price": float(raw_close),
                    "currency": instrument["currency"],
                    "source": "yahoo_chart_close",
                }
            )
        if adjusted_close not in (None, 0):
            rows.append(
                {
                    "instrument_id": instrument["id"],
                    "price_date": price_date,
                    "close_price": float(adjusted_close),
                    "currency": instrument["currency"],
                    "source": "yahoo_chart_adjusted",
                }
            )
    return rows


def _fetch_sina_history(
    session: requests.Session,
    instrument: dict,
    sina_symbol: str,
    start_date: date | None = None,
    request_timeout: int = 30,
) -> list[dict]:
    if start_date is None:
        datalen = 4000
    else:
        span_days = max(30, (datetime.now().date() - start_date).days + 10)
        datalen = min(4000, span_days)
    response = session.get(
        SINA_KLINE_URL,
        params={
            "symbol": sina_symbol,
            "scale": "240",
            "ma": "no",
            "datalen": str(datalen),
        },
        headers={"Referer": "https://finance.sina.com.cn/"},
        timeout=request_timeout,
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, list):
        raise ValueError("Unexpected Sina K-line payload")

    rows = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        day = item.get("day")
        close_price = item.get("close")
        if not day or close_price in (None, "", "0", 0):
            continue
        rows.append(
            {
                "instrument_id": instrument["id"],
                "price_date": str(day),
                "close_price": float(close_price),
                "currency": instrument["currency"],
                "source": "sina_kline",
            }
        )
    return rows


def _fetch_eastmoney_fund_history(
    session: requests.Session,
    instrument: dict,
    fund_code: str,
    start_date: str = "2000-01-01",
    request_timeout: int = 30,
) -> list[dict]:
    rows = []
    page_index = 1
    while True:
        response = session.get(
            EASTMONEY_FUND_NAV_URL,
            params={
                "fundCode": fund_code,
                "pageIndex": page_index,
                "pageSize": 20,
                "startDate": start_date,
                "endDate": datetime.now().date().isoformat(),
            },
            headers={"Referer": f"https://fundf10.eastmoney.com/jjjz_{fund_code}.html"},
            timeout=request_timeout,
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("ErrCode") not in (0, None):
            raise ValueError(payload.get("ErrMsg") or f"Eastmoney returned ErrCode={payload.get('ErrCode')}")

        data = payload.get("Data") or {}
        items = data.get("LSJZList") or []
        for item in items:
            if not isinstance(item, dict):
                continue
            price_date = item.get("FSRQ")
            unit_nav = item.get("DWJZ")
            if not price_date or unit_nav in (None, "", 0, "0"):
                continue
            rows.append(
                {
                    "instrument_id": instrument["id"],
                    "price_date": price_date,
                    "close_price": float(unit_nav),
                    "currency": instrument["currency"],
                    "source": "eastmoney_fund_nav",
                }
            )

        total_count = int(payload.get("TotalCount") or len(items))
        page_size = int(payload.get("PageSize") or 20)
        if not items or page_index * page_size >= total_count:
            break
        page_index += 1
    return rows


def _parse_jsonp_payload(text: str) -> dict:
    cleaned = text.strip()
    match = re.match(r"^[^(]+\((.*)\)\s*;?\s*$", cleaned, re.S)
    if match:
        cleaned = match.group(1)
    payload = json.loads(cleaned)
    if not isinstance(payload, dict):
        raise ValueError("Unexpected JSONP payload shape")
    return payload


def _yahoo_symbol_for_instrument(instrument: dict) -> str | None:
    symbol = (instrument.get("symbol") or "").strip()
    if not symbol:
        return None
    if instrument.get("broker") == "ibkr":
        if instrument.get("currency") == "USD":
            return symbol.split()[0]
        if instrument.get("market") == "HK" and symbol.isdigit():
            return f"{int(symbol):04d}.HK"
        return None

    if instrument.get("broker") == "gtja":
        digits = symbol.zfill(6) if symbol.isdigit() else symbol
        market = instrument.get("market")
        if market == "CN" and digits.isdigit():
            suffix = "SS" if digits.startswith(("5", "6", "9")) else "SZ"
            return f"{digits}.{suffix}"
        if market == "HK" and digits.isdigit():
            return f"{int(digits):04d}.HK"
    return None


def _sina_symbol_for_instrument(instrument: dict) -> str | None:
    if instrument.get("broker") != "gtja" or instrument.get("market") != "CN":
        return None
    symbol = (instrument.get("symbol") or "").strip()
    if not re.fullmatch(r"\d{6}", symbol):
        return None
    if symbol.startswith(("5", "6", "9", "1")):
        return f"sh{symbol}"
    return f"sz{symbol}"


def _eastmoney_fund_code_for_instrument(instrument: dict) -> str | None:
    if instrument.get("broker") != "gtja":
        return None
    if instrument.get("market") != "CN":
        return None
    if instrument.get("asset_class") != "fund":
        return None
    if not instrument.get("has_confirm"):
        return None
    symbol = (instrument.get("symbol") or "").strip()
    if not re.fullmatch(r"\d{6}", symbol):
        return None
    # Exchange-traded funds should stay on market close prices instead of off-exchange NAVs.
    if symbol.startswith("5"):
        return None
    return symbol
