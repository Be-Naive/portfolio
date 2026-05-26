from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
import json
from typing import Dict, Iterable, List, Optional, Tuple

from . import db


BUY_ACTIVITIES = {"security_buy", "fund_subscription_confirm", "fund_subscription_fund_out", "repo_open"}
SELL_ACTIVITIES = {"security_sell", "fund_redemption_in", "repo_close"}
QUANTITY_ONLY_ACTIVITIES = {"stock_split", "position_adjustment"}
QUANTITY_REDUCTION_ONLY_ACTIVITIES = {"position_adjustment_out"}
PRICE_SOURCE_PRIORITY = {
    "gtja_transaction": 10,
    "ibkr_flex_trade": 20,
    "ibkr_flex_position": 30,
    "eastmoney_fund_nav": 35,
    "yahoo_chart": 36,
    "yahoo_chart_adjusted": 36,
    "yahoo_chart_close": 38,
    "sina_kline": 40,
    "manual_csv": 50,
}
BOOTSTRAP_PRICE_SOURCES = {"gtja_transaction", "ibkr_flex_trade"}
BENCHMARK_PRICE_SOURCE_PRIORITY = {
    "yahoo_chart": 50,
    "yahoo_chart_adjusted": 50,
    "yahoo_chart_close": 40,
    "sina_kline": 35,
    "eastmoney_fund_nav": 30,
    "ibkr_flex_trade": 20,
    "gtja_transaction": 10,
}
FX_SOURCE_PRIORITY = {
    "ibkr_flex_conversion": 20,
    "ecb_reference": 40,
}
NO_POSITION_ACTIVITIES = {
    "bank_transfer_in",
    "bank_transfer_out",
    "fx_conversion",
    "history_migration",
    "ipo_lottery",
    "dividend_tax",
    "interest_credit",
    "dividend",
    "corporate_action_cash",
    "corporate_action",
}
NO_CASH_EFFECT_ACTIVITIES = {"history_migration"}
EXCLUDED_EFFECTIVE_ASSET_CLASSES = {"cash_management", "repo"}
EXCLUDED_BENCHMARK_ASSET_CLASSES = {"cash_management", "repo"}


@dataclass
class TransactionEffect:
    quantity_delta: float
    cash_delta: float
    trade_value: float


def build_dashboard(connection, base_currency: str = "CNY") -> Dict[str, object]:
    transactions = db.fetch_all(
        connection,
        """
        SELECT
            t.*,
            a.display_name AS account_name,
            i.symbol,
            i.name,
            i.asset_class,
            i.market
        FROM transactions t
        JOIN accounts a ON a.id = t.account_id
        LEFT JOIN instruments i ON i.id = t.instrument_id
        ORDER BY t.trade_date, t.rowid
        """,
    )
    latest_positions = _latest_position_snapshots(connection)
    latest_cash = _latest_cash_balances(connection)
    price_rows = _price_history_rows(connection)
    fx_rows = _fx_history_rows(connection)
    price_map = _latest_prices(price_rows)
    fx_pairs = _latest_fx_pairs(fx_rows)

    holdings, cash_totals = _rebuild_holdings(transactions, latest_positions, latest_cash, fx_pairs, base_currency)
    products = _product_analysis(holdings, price_map, fx_pairs, base_currency)
    timeseries = _build_timeseries(transactions, price_rows, fx_rows, base_currency)
    profit_series = []
    for nav_point, contribution_point in zip(timeseries["nav"], timeseries["net_contribution"]):
        profit_series.append(
            {
                "date": nav_point["date"],
                "value": round(nav_point["value"] - contribution_point["value"], 2),
            }
        )
    timeseries["profit"] = profit_series

    total_market_value = sum(item["market_value_base"] for item in products) + cash_totals["base_total"]
    net_contribution = timeseries["net_contribution"][-1]["value"] if timeseries["net_contribution"] else 0.0
    total_return = total_market_value - net_contribution
    allocation = _allocation(products, cash_totals, base_currency, fx_pairs)
    product_views = _decorate_products(products, total_market_value)
    transactions_view = _decorate_transactions(transactions)
    rebalance = suggest_rebalance(allocation["product"], cash_totals["base_total"], total_market_value)
    benchmark_choices, benchmark_series = _benchmark_catalog(products, price_rows)

    return {
        "summary": {
            "base_currency": base_currency,
            "total_market_value": round(total_market_value, 2),
            "cash_total": round(cash_totals["base_total"], 2),
            "net_contribution": round(net_contribution, 2),
            "total_return": round(total_return, 2),
            "imported_transactions": len(transactions),
            "products": len(products),
        },
        "timeseries": timeseries,
        "products": product_views,
        "allocation": allocation,
        "rebalance": rebalance,
        "benchmark_choices": benchmark_choices,
        "benchmark_series": benchmark_series,
        "transactions": transactions_view,
        "ibkr_status": _latest_sync_status(connection),
    }


def suggest_rebalance(
    asset_allocations: Iterable[Dict[str, object]],
    available_cash_base: float,
    total_value_base: float,
    targets: Optional[Dict[str, float]] = None,
) -> Dict[str, object]:
    items = [dict(item) for item in asset_allocations if item.get("market_value_base", 0.0) > 0]
    if not items:
        return {"targets": {}, "actions": [], "drift": []}

    universe_total = sum(item["market_value_base"] for item in items)
    if universe_total <= 0:
        return {"targets": {}, "actions": [], "drift": []}

    if targets is None:
        targets = {item["label"]: round((item["market_value_base"] / universe_total) * 100.0, 2) for item in items}

    target_total = sum(targets.values()) or 100.0
    normalized_targets = {key: value / target_total for key, value in targets.items()}
    drift: List[Dict[str, object]] = []
    buy_needs: List[Tuple[str, float]] = []
    sell_excess: List[Tuple[str, float]] = []
    for item in items:
        target_pct = normalized_targets.get(item["label"], 0.0)
        target_value = universe_total * target_pct
        delta = target_value - item["market_value_base"]
        drift.append(
            {
                "label": item["label"],
                "current_percent": round((item["market_value_base"] / universe_total) * 100, 2) if universe_total else 0.0,
                "target_percent": round(target_pct * 100, 2),
                "delta_value": round(delta, 2),
            }
        )
        if delta > 1:
            buy_needs.append((item["label"], delta))
        elif delta < -1:
            sell_excess.append((item["label"], -delta))

    actions: List[Dict[str, object]] = []
    for label, amount in sorted(buy_needs, key=lambda pair: pair[1], reverse=True):
        if amount >= 100:
            actions.append(
                {
                    "action": "buy",
                    "bucket": label,
                    "amount_base": round(amount, 2),
                    "reason": "Buy this much inside the selected rebalance subset.",
                }
            )

    for label, amount in sorted(sell_excess, key=lambda pair: pair[1], reverse=True):
        if amount >= 100:
            actions.append(
                {
                    "action": "sell",
                    "bucket": label,
                    "amount_base": round(amount, 2),
                    "reason": "Sell this much to fund the selected-subset rebalance.",
                }
            )

    return {
        "targets": {key: round(value * 100, 2) for key, value in normalized_targets.items()},
        "drift": drift,
        "actions": actions,
        "subset_total": round(universe_total, 2),
    }


def parse_rebalance_targets(raw: str) -> Dict[str, float]:
    targets: Dict[str, float] = {}
    for line in raw.splitlines():
        cleaned = line.strip()
        if not cleaned:
            continue
        if ":" in cleaned:
            key, value = cleaned.split(":", 1)
        elif "=" in cleaned:
            key, value = cleaned.split("=", 1)
        else:
            parts = cleaned.split()
            if len(parts) != 2:
                continue
            key, value = parts
        targets[key.strip()] = float(value.strip().replace("%", ""))
    return targets


def _latest_position_snapshots(connection) -> List[dict]:
    latest = db.fetch_one(
        connection,
        "SELECT MAX(snapshot_time) AS snapshot_time FROM position_snapshots",
    )
    if not latest or not latest["snapshot_time"]:
        return []
    rows = db.fetch_all(
        connection,
        """
        SELECT ps.*, i.symbol, i.name, i.asset_class, i.market
        FROM position_snapshots ps
        JOIN instruments i ON i.id = ps.instrument_id
        WHERE ps.snapshot_time = ?
        """,
        [latest["snapshot_time"]],
    )
    return [dict(row) for row in rows]


def _latest_cash_balances(connection) -> List[dict]:
    latest = db.fetch_one(connection, "SELECT MAX(snapshot_time) AS snapshot_time FROM cash_balances")
    if not latest or not latest["snapshot_time"]:
        return []
    rows = db.fetch_all(
        connection,
        "SELECT * FROM cash_balances WHERE snapshot_time = ?",
        [latest["snapshot_time"]],
    )
    return [dict(row) for row in rows]


def _price_history_rows(connection) -> List[dict]:
    rows = [
        dict(row)
        for row in db.fetch_all(
            connection,
            """
            SELECT ph.instrument_id, ph.price_date, ph.close_price, ph.currency, ph.source, i.asset_class
            FROM price_history ph
            LEFT JOIN instruments i ON i.id = ph.instrument_id
            """,
        )
    ]
    rows = _filter_bootstrap_price_rows(rows)
    return sorted(
        rows,
        key=lambda row: (row["price_date"], PRICE_SOURCE_PRIORITY.get(row["source"], 0), row["instrument_id"]),
    )


def _filter_bootstrap_price_rows(price_rows: List[dict]) -> List[dict]:
    first_market_date_by_instrument: Dict[str, str] = {}
    for row in price_rows:
        if row["source"] in BOOTSTRAP_PRICE_SOURCES:
            continue
        instrument_id = row["instrument_id"]
        existing = first_market_date_by_instrument.get(instrument_id)
        if existing is None or row["price_date"] < existing:
            first_market_date_by_instrument[instrument_id] = row["price_date"]

    filtered = []
    for row in price_rows:
        first_market_date = first_market_date_by_instrument.get(row["instrument_id"])
        if (
            row["source"] in BOOTSTRAP_PRICE_SOURCES
            and first_market_date is not None
            and row["price_date"] >= first_market_date
        ):
            continue
        filtered.append(row)
    return filtered


def _fx_history_rows(connection) -> List[dict]:
    rows = [dict(row) for row in db.fetch_all(connection, "SELECT rate_date, base_currency, quote_currency, rate, source FROM fx_rates")]
    return sorted(
        rows,
        key=lambda row: (row["rate_date"], FX_SOURCE_PRIORITY.get(row["source"], 0), row["base_currency"], row["quote_currency"]),
    )


def _latest_prices(price_rows: List[dict]) -> Dict[str, Tuple[float, str]]:
    latest: Dict[str, Tuple[float, str]] = {}
    for row in price_rows:
        latest[row["instrument_id"]] = (row["close_price"], row["currency"])
    return latest


def _latest_fx_pairs(fx_rows: List[dict]) -> Dict[Tuple[str, str], float]:
    latest: Dict[Tuple[str, str], float] = {}
    for row in fx_rows:
        latest[(row["base_currency"], row["quote_currency"])] = row["rate"]
    return latest


def _rebuild_holdings(transactions, position_snapshots, cash_snapshots, fx_pairs, base_currency):
    holdings: Dict[str, Dict[str, object]] = {}
    latest_cash_by_account_currency = {}
    running_cash_by_account_currency = defaultdict(float)
    pending_asset_by_account_currency = defaultdict(float)
    pending_subscriptions_by_instrument = defaultdict(float)
    pending_redemptions_by_instrument = defaultdict(float)
    pending_redemption_quantities_by_instrument = defaultdict(float)
    pending_redemption_fees_by_instrument = defaultdict(float)

    for row in transactions:
        normalized_row = dict(row)
        quantity_override = _special_fund_flow_quantity_override(
            normalized_row,
            pending_asset_by_account_currency,
            pending_subscriptions_by_instrument,
            pending_redemptions_by_instrument,
            pending_redemption_quantities_by_instrument,
            pending_redemption_fees_by_instrument,
        )
        effect = _transaction_effect(normalized_row)
        if quantity_override is not None:
            effect = TransactionEffect(
                quantity_delta=quantity_override,
                cash_delta=effect.cash_delta,
                trade_value=effect.trade_value,
            )
        row = normalized_row
        currency = row["currency"] or "CNY"
        for cash_currency, delta in _cash_effects(normalized_row).items():
            running_cash_by_account_currency[(row["account_id"], cash_currency)] += delta
        if row["cash_balance"] is not None:
            latest_cash_by_account_currency[(row["account_id"], currency)] = float(row["cash_balance"])
        instrument_id = row["instrument_id"]
        if not instrument_id or row["activity_type"] in NO_POSITION_ACTIVITIES:
            continue
        bucket = holdings.setdefault(
            instrument_id,
            {
                "instrument_id": instrument_id,
                "symbol": row["symbol"] or "",
                "name": row["name"] or row["description"],
                "asset_class": row["asset_class"] or "other",
                "market": row["market"] or "UNKNOWN",
                "currency": currency,
                "quantity": 0.0,
                "average_cost": 0.0,
                "cost_basis_total": 0.0,
                "realized_pnl": 0.0,
                "account_name": row["account_name"],
            },
        )
        _seed_opening_quantity(bucket, normalized_row, effect)

        if effect.quantity_delta > 0:
            if row["activity_type"] in QUANTITY_ONLY_ACTIVITIES:
                bucket["quantity"] += effect.quantity_delta
                bucket["average_cost"] = bucket["cost_basis_total"] / bucket["quantity"] if bucket["quantity"] else 0.0
                continue
            acquisition_cost = row["gross_amount"] or abs(row["cash_amount"] or 0.0)
            if acquisition_cost == 0.0 and row.get("asset_class") == "cash_management":
                acquisition_cost = abs(effect.quantity_delta) * (float(row.get("price") or 1.0) or 1.0)
            acquisition_cost += (row["commission_total"] or 0.0) + (row["stamp_duty"] or 0.0) + (row["transfer_fee"] or 0.0) + (row["other_fee"] or 0.0)
            bucket["quantity"] += effect.quantity_delta
            bucket["cost_basis_total"] += acquisition_cost
            bucket["average_cost"] = bucket["cost_basis_total"] / bucket["quantity"] if bucket["quantity"] else 0.0
        elif effect.quantity_delta < 0 and bucket["quantity"] > 0:
            if row["activity_type"] in QUANTITY_ONLY_ACTIVITIES or row["activity_type"] in QUANTITY_REDUCTION_ONLY_ACTIVITIES:
                bucket["quantity"] += effect.quantity_delta
                if row["activity_type"] in QUANTITY_REDUCTION_ONLY_ACTIVITIES:
                    if bucket["quantity"] > 0:
                        removed_ratio = abs(effect.quantity_delta) / max(bucket["quantity"] + abs(effect.quantity_delta), 1e-9)
                        bucket["cost_basis_total"] *= max(0.0, 1.0 - removed_ratio)
                    else:
                        bucket["cost_basis_total"] = 0.0
                bucket["average_cost"] = bucket["cost_basis_total"] / bucket["quantity"] if bucket["quantity"] else 0.0
                continue
            quantity_sold = abs(effect.quantity_delta)
            average_cost = bucket["average_cost"]
            cost_removed = average_cost * quantity_sold
            proceeds = row["gross_amount"] or abs(row["cash_amount"] or 0.0)
            proceeds -= (row["commission_total"] or 0.0) + (row["stamp_duty"] or 0.0) + (row["transfer_fee"] or 0.0) + (row["other_fee"] or 0.0)
            bucket["quantity"] -= quantity_sold
            bucket["cost_basis_total"] = max(0.0, bucket["cost_basis_total"] - cost_removed)
            bucket["realized_pnl"] += proceeds - cost_removed
            bucket["average_cost"] = bucket["cost_basis_total"] / bucket["quantity"] if bucket["quantity"] else 0.0

    for snapshot in position_snapshots:
        holdings[snapshot["instrument_id"]] = {
            "instrument_id": snapshot["instrument_id"],
            "symbol": snapshot["symbol"],
            "name": snapshot["name"],
            "asset_class": snapshot["asset_class"],
            "market": snapshot["market"],
            "currency": snapshot["currency"],
            "quantity": snapshot["quantity"],
            "average_cost": snapshot["average_cost"] or 0.0,
            "cost_basis_total": (snapshot["average_cost"] or 0.0) * (snapshot["quantity"] or 0.0),
            "realized_pnl": 0.0,
            "account_name": snapshot["account_id"],
            "market_price": snapshot["market_price"] or 0.0,
            "market_value": snapshot["market_value"] or 0.0,
            "unrealized_pnl": snapshot["unrealized_pnl"] or 0.0,
        }

    for snapshot in cash_snapshots:
        latest_cash_by_account_currency[(snapshot["account_id"], snapshot["currency"])] = snapshot["amount"] or 0.0

    merged_cash_by_account_currency = dict(running_cash_by_account_currency)
    merged_cash_by_account_currency.update(latest_cash_by_account_currency)
    cash_by_currency = defaultdict(float)
    for (_account_id, currency), amount in merged_cash_by_account_currency.items():
        cash_by_currency[currency] += amount
    for (_account_id, currency), amount in pending_asset_by_account_currency.items():
        cash_by_currency[currency] += amount

    base_total = 0.0
    for currency, amount in cash_by_currency.items():
        base_total += amount * _resolve_fx_rate(currency, base_currency, fx_pairs)

    return holdings, {"by_currency": dict(cash_by_currency), "base_total": round(base_total, 2)}


def _product_analysis(holdings, price_map, fx_pairs, base_currency):
    products = []
    for instrument_id, bucket in holdings.items():
        quantity = bucket["quantity"]
        if quantity <= 0 and abs(bucket["realized_pnl"]) < 0.01:
            continue
        price, price_currency = price_map.get(instrument_id, (bucket.get("market_price", 0.0), bucket["currency"]))
        unit_multiplier = 100.0 if bucket["asset_class"] == "option" else 1.0
        if bucket["asset_class"] == "repo":
            price = 1.0
            price_currency = bucket["currency"]
        if bucket["asset_class"] == "cash_management":
            price = price or 1.0
            price_currency = bucket["currency"]
        native_currency = price_currency or bucket["currency"]
        fx_rate = _resolve_fx_rate(native_currency, base_currency, fx_pairs)
        market_value = bucket.get("market_value") or quantity * price * unit_multiplier
        market_value_base = market_value * fx_rate
        unrealized = bucket.get("unrealized_pnl")
        if quantity <= 0:
            unrealized = 0.0
        elif unrealized in (None, 0.0):
            unrealized = market_value - bucket["cost_basis_total"]
        realized_native = bucket["realized_pnl"]
        unrealized_native = unrealized
        total_return_native = realized_native + unrealized_native
        products.append(
            {
                **bucket,
                "display_label": _display_label(bucket["name"], bucket["symbol"]),
                "status": "closed" if quantity <= 0 else "open",
                "price": round(price, 4),
                "price_currency": native_currency,
                "native_currency": native_currency,
                "fx_rate_to_base": round(fx_rate, 6),
                "market_value": round(market_value, 2),
                "market_value_base": round(market_value_base, 2),
                "realized_pnl": round(realized_native, 2),
                "realized_pnl_base": round(realized_native * fx_rate, 2),
                "unrealized_pnl": round(unrealized_native, 2),
                "unrealized_pnl_base": round(unrealized_native * fx_rate, 2),
                "total_return": round(total_return_native, 2),
                "total_return_base": round(total_return_native * fx_rate, 2),
            }
        )
    return sorted(products, key=lambda item: item["market_value_base"], reverse=True)


def _display_label(name: str, symbol: str) -> str:
    cleaned_name = (name or "").strip()
    cleaned_symbol = (symbol or "").strip()
    if cleaned_symbol and cleaned_symbol != cleaned_name:
        return f"{cleaned_name} ({cleaned_symbol})" if cleaned_name else cleaned_symbol
    return cleaned_name or cleaned_symbol or "未命名产品"


def _benchmark_catalog(products, price_rows):
    product_by_instrument = {
        item["instrument_id"]: item
        for item in products
        if item["asset_class"] not in EXCLUDED_BENCHMARK_ASSET_CLASSES
        and "SPLIT" not in item["display_label"].upper()
    }
    if not product_by_instrument:
        return [], {}

    by_instrument_date: Dict[str, Dict[str, dict]] = defaultdict(dict)
    for row in price_rows:
        instrument_id = row["instrument_id"]
        if instrument_id not in product_by_instrument:
            continue
        existing = by_instrument_date[instrument_id].get(row["price_date"])
        if existing and BENCHMARK_PRICE_SOURCE_PRIORITY.get(existing["source"], 0) > BENCHMARK_PRICE_SOURCE_PRIORITY.get(row["source"], 0):
            continue
        by_instrument_date[instrument_id][row["price_date"]] = {
            "date": row["price_date"],
            "value": round(float(row["close_price"]), 6),
            "source": row["source"],
        }

    benchmark_choices = []
    benchmark_series = {}
    for instrument_id, product in product_by_instrument.items():
        raw_series = sorted(by_instrument_date.get(instrument_id, {}).values(), key=lambda item: item["date"])
        if len(raw_series) < 2:
            continue
        benchmark_choices.append(
            {
                "id": instrument_id,
                "label": product["display_label"],
                "currency": product["price_currency"],
                "status": product["status"],
            }
        )
        benchmark_series[instrument_id] = {
            "label": product["display_label"],
            "currency": product["price_currency"],
            "series": [{"date": item["date"], "value": item["value"]} for item in raw_series],
        }
    benchmark_choices.sort(key=lambda item: (item["status"] != "open", item["label"]))
    return benchmark_choices, benchmark_series


def _build_timeseries(transactions, price_rows, fx_rows, base_currency):
    if not transactions:
        return {"nav": [], "net_contribution": []}

    start = datetime.strptime(transactions[0]["trade_date"], "%Y-%m-%d").date()
    end = date.today()
    position_state = defaultdict(float)
    cash_state = defaultdict(float)
    effective_bridge_by_currency = defaultdict(float)
    pending_asset_by_currency = defaultdict(float)
    pending_subscriptions_by_instrument = defaultdict(float)
    pending_redemptions_by_instrument = defaultdict(float)
    pending_redemption_quantities_by_instrument = defaultdict(float)
    pending_redemption_fees_by_instrument = defaultdict(float)
    price_state: Dict[str, Tuple[float, str]] = {}
    instrument_state: Dict[str, Dict[str, str]] = {}
    fx_state: Dict[Tuple[str, str], float] = {}
    nav_series = []
    contribution_series = []
    total_twr_series = []
    effective_twr_series = []
    peak_cost_rate_series = []
    tx_by_date = defaultdict(list)
    for row in transactions:
        tx_by_date[row["trade_date"]].append(dict(row))
    price_by_date = defaultdict(list)
    for row in price_rows:
        price_by_date[row["price_date"]].append(row)
    fx_by_date = defaultdict(list)
    for row in fx_rows:
        fx_by_date[row["rate_date"]].append(row)

    running_contribution = 0.0
    cumulative_total_twr = 1.0
    cumulative_effective_twr = 1.0
    previous_total_nav = 0.0
    previous_effective_nav = 0.0
    running_peak_contribution = 0.0
    current = start
    while current <= end:
        day_key = current.isoformat()
        day_total_flow_base = 0.0
        day_effective_flow_base = 0.0
        for row in fx_by_date.get(day_key, []):
            fx_state[(row["base_currency"], row["quote_currency"])] = row["rate"]
        for row in tx_by_date.get(day_key, []):
            row = _enrich_fund_flow_row(dict(row), price_state)
            quantity_override = _special_fund_flow_quantity_override(
                row,
                pending_asset_by_currency,
                pending_subscriptions_by_instrument,
                pending_redemptions_by_instrument,
                pending_redemption_quantities_by_instrument,
                pending_redemption_fees_by_instrument,
                account_key=False,
            )
            effect = _transaction_effect(row)
            if quantity_override is not None:
                effect = TransactionEffect(
                    quantity_delta=quantity_override,
                    cash_delta=effect.cash_delta,
                    trade_value=effect.trade_value,
                )
            currency = row["currency"] or "CNY"
            for cash_currency, delta in _cash_effects(row).items():
                cash_state[cash_currency] += delta
            bridge_delta = _effective_bridge_cash_delta(row)
            if bridge_delta:
                effective_bridge_by_currency[currency] += bridge_delta
                day_effective_flow_base += bridge_delta * _resolve_fx_rate(currency, base_currency, fx_state)
            if row["external_flow"]:
                flow_base = (row["cash_amount"] or 0.0) * _resolve_fx_rate(currency, base_currency, fx_state)
                running_contribution += flow_base
                day_total_flow_base += flow_base
                day_effective_flow_base += flow_base
            instrument_id = row["instrument_id"]
            if instrument_id and row["activity_type"] not in NO_POSITION_ACTIVITIES:
                instrument_state[instrument_id] = {
                    "asset_class": row.get("asset_class") or "other",
                    "currency": currency,
                }
                opening_value = _seed_opening_position_state(position_state, row, effect)
                if opening_value > 0:
                    opening_value_base = opening_value * _resolve_fx_rate(currency, base_currency, fx_state)
                    running_contribution += opening_value_base
                    day_total_flow_base += opening_value_base
                    if row.get("asset_class") not in EXCLUDED_EFFECTIVE_ASSET_CLASSES:
                        day_effective_flow_base += opening_value_base
                position_state[instrument_id] += effect.quantity_delta
                day_effective_flow_base += _effective_boundary_flow_base(row, effect, price_state, base_currency, fx_state)
                if row["price"]:
                    if row.get("asset_class") == "repo":
                        price_state[instrument_id] = (1.0, currency)
                    else:
                        price_state[instrument_id] = (row["price"], currency)
        for row in price_by_date.get(day_key, []):
            instrument_state.setdefault(
                row["instrument_id"],
                {"asset_class": row.get("asset_class") or "other", "currency": row["currency"]},
            )
            if row.get("asset_class") == "repo":
                price_state[row["instrument_id"]] = (1.0, row["currency"])
            else:
                price_state[row["instrument_id"]] = (row["close_price"], row["currency"])

        nav_total = 0.0
        excluded_effective_nav = 0.0
        for instrument_id, quantity in position_state.items():
            if quantity <= 0:
                continue
            instrument_meta = instrument_state.get(instrument_id, {})
            price, currency = price_state.get(instrument_id, (0.0, instrument_meta.get("currency", "CNY")))
            if not price and instrument_meta.get("asset_class") == "cash_management":
                price = 1.0
            unit_multiplier = 100.0 if instrument_meta.get("asset_class") == "option" else 1.0
            market_value_base = quantity * price * unit_multiplier * _resolve_fx_rate(currency, base_currency, fx_state)
            nav_total += market_value_base
            if instrument_meta.get("asset_class") in EXCLUDED_EFFECTIVE_ASSET_CLASSES:
                excluded_effective_nav += market_value_base
        for currency, amount in cash_state.items():
            nav_total += amount * _resolve_fx_rate(currency, base_currency, fx_state)
        for currency, amount in pending_asset_by_currency.items():
            nav_total += amount * _resolve_fx_rate(currency, base_currency, fx_state)
        effective_bridge_nav = 0.0
        for currency, amount in effective_bridge_by_currency.items():
            effective_bridge_nav += amount * _resolve_fx_rate(currency, base_currency, fx_state)
        effective_nav = nav_total - excluded_effective_nav + effective_bridge_nav
        daily_total_twr = _modified_dietz_return(previous_total_nav, nav_total, day_total_flow_base)
        daily_effective_twr = _modified_dietz_return(previous_effective_nav, effective_nav, day_effective_flow_base)
        cumulative_total_twr *= 1.0 + daily_total_twr
        cumulative_effective_twr *= 1.0 + daily_effective_twr
        running_peak_contribution = max(running_peak_contribution, running_contribution)
        profit_value = nav_total - running_contribution
        nav_series.append({"date": day_key, "value": round(nav_total, 2)})
        contribution_series.append({"date": day_key, "value": round(running_contribution, 2)})
        total_twr_series.append({"date": day_key, "value": round((cumulative_total_twr - 1.0) * 100.0, 4)})
        effective_twr_series.append({"date": day_key, "value": round((cumulative_effective_twr - 1.0) * 100.0, 4)})
        peak_cost_rate_series.append(
            {
                "date": day_key,
                "value": round((profit_value / running_peak_contribution) * 100.0, 4) if running_peak_contribution > 0 else 0.0,
            }
        )
        previous_total_nav = nav_total
        previous_effective_nav = effective_nav
        current += timedelta(days=1)
    return {
        "nav": nav_series,
        "net_contribution": contribution_series,
        "total_twr": total_twr_series,
        "effective_twr": effective_twr_series,
        "peak_cost_rate": peak_cost_rate_series,
    }


def _enrich_fund_flow_row(row: Dict[str, object], price_state: Dict[str, Tuple[float, str]]) -> Dict[str, object]:
    if row.get("activity_type") != "fund_redemption_in":
        return row

    quantity = float(row.get("quantity") or 0.0)
    gross_amount = float(row.get("gross_amount") or 0.0)
    cash_amount = float(row.get("cash_amount") or 0.0)
    instrument_id = row.get("instrument_id")
    asset_class = row.get("asset_class")
    currency = row.get("currency") or "CNY"

    if quantity > 0.0 and gross_amount == 0.0 and cash_amount == 0.0 and instrument_id:
        price, price_currency = price_state.get(instrument_id, (0.0, currency))
        if asset_class == "cash_management" and price == 0.0:
            price = 1.0
            price_currency = currency
        if price > 0.0:
            row["gross_amount"] = round(quantity * price, 6)
            if not row.get("price"):
                row["price"] = price
            row["currency"] = price_currency or currency

    if quantity == 0.0 and gross_amount == 0.0 and cash_amount > 0.0:
        row["gross_amount"] = abs(cash_amount)

    return row


def _allocation(products, cash_totals, base_currency, fx_pairs):
    cash_by_currency = cash_totals["by_currency"]
    total_products = sum(item["market_value_base"] for item in products)
    total_cash = cash_totals["base_total"]
    base_total = total_products + total_cash

    asset_class = defaultdict(float)
    markets = defaultdict(float)
    product_rows = []
    for item in products:
        asset_class[item["asset_class"]] += item["market_value_base"]
        markets[item["market"]] += item["market_value_base"]
        if item["status"] == "open" and item["market_value_base"] > 0:
            product_rows.append(
                {
                    "label": item["display_label"],
                    "name": item["name"],
                    "symbol": item["symbol"],
                    "asset_class": item["asset_class"],
                    "market": item["market"],
                    "market_value_base": round(item["market_value_base"], 2),
                    "weight": (item["market_value_base"] / base_total) if base_total else 0.0,
                }
            )
    for currency, amount in cash_by_currency.items():
        converted = amount * _resolve_fx_rate(currency, base_currency, fx_pairs)
        asset_class["cash"] += converted
        markets[currency] += converted

    by_asset_class = [
        {
            "label": label,
            "market_value_base": round(value, 2),
            "weight": (value / base_total) if base_total else 0.0,
        }
        for label, value in sorted(asset_class.items(), key=lambda pair: pair[1], reverse=True)
    ]
    by_market = [
        {
            "label": label,
            "market_value_base": round(value, 2),
            "weight": (value / base_total) if base_total else 0.0,
        }
        for label, value in sorted(markets.items(), key=lambda pair: pair[1], reverse=True)
    ]
    return {
        "product": sorted(product_rows, key=lambda item: item["market_value_base"], reverse=True),
        "asset_class": by_asset_class,
        "market": by_market,
        "cash_by_currency": cash_by_currency,
        "base_total": round(base_total, 2),
    }


def _decorate_products(products, total_market_value):
    for item in products:
        item["weight"] = round((item["market_value_base"] / total_market_value) * 100, 2) if total_market_value else 0.0
    return products


def _decorate_transactions(rows):
    rendered = []
    for row in rows[-20:]:
        rendered.append(
            {
                "date": row["trade_date"],
                "account": row["account_name"],
                "activity": row["activity_type"],
                "symbol": row["symbol"] or "",
                "name": row["name"] or row["description"],
                "quantity": row["quantity"] or 0.0,
                "cash_amount": row["cash_amount"] or 0.0,
                "currency": row["currency"],
            }
        )
    return list(reversed(rendered))


def _latest_sync_status(connection):
    row = db.fetch_one(
        connection,
        "SELECT * FROM sync_runs ORDER BY started_at DESC LIMIT 1",
    )
    return dict(row) if row else None


def _transaction_effect(row: Dict[str, object]) -> TransactionEffect:
    if row.get("asset_class") == "repo":
        principal = float(row.get("gross_amount") or 0.0)
        if row["activity_type"] == "repo_open":
            return TransactionEffect(quantity_delta=principal, cash_delta=float(row.get("cash_amount") or 0.0), trade_value=principal)
        if row["activity_type"] == "repo_close":
            return TransactionEffect(quantity_delta=-principal, cash_delta=float(row.get("cash_amount") or 0.0), trade_value=principal)
    quantity = float(row.get("quantity") or 0.0)
    cash_delta = float(row.get("cash_amount") or 0.0)
    if row["activity_type"] in BUY_ACTIVITIES:
        return TransactionEffect(quantity_delta=quantity, cash_delta=cash_delta, trade_value=float(row.get("gross_amount") or 0.0))
    if row["activity_type"] in SELL_ACTIVITIES:
        return TransactionEffect(quantity_delta=-quantity, cash_delta=cash_delta, trade_value=float(row.get("gross_amount") or 0.0))
    if row["activity_type"] in QUANTITY_ONLY_ACTIVITIES:
        return TransactionEffect(quantity_delta=quantity, cash_delta=cash_delta, trade_value=0.0)
    if row["activity_type"] in QUANTITY_REDUCTION_ONLY_ACTIVITIES:
        return TransactionEffect(quantity_delta=-quantity, cash_delta=cash_delta, trade_value=0.0)
    return TransactionEffect(quantity_delta=0.0, cash_delta=cash_delta, trade_value=float(row.get("gross_amount") or 0.0))


def _cash_effects(row: Dict[str, object]) -> Dict[str, float]:
    if row.get("activity_type") in NO_CASH_EFFECT_ACTIVITIES:
        return {}
    currency = row.get("currency") or "CNY"
    cash_amount = float(row.get("cash_amount") or 0.0)
    if row.get("activity_type") != "fx_conversion":
        return {currency: cash_amount} if cash_amount else {}

    raw_json = row.get("raw_json")
    if isinstance(raw_json, str) and raw_json:
        try:
            raw = json.loads(raw_json)
        except json.JSONDecodeError:
            raw = {}
    elif isinstance(raw_json, dict):
        raw = raw_json
    else:
        raw = {}
    symbol = (row.get("symbol") or raw.get("symbol") or "").strip()
    quantity = float(row.get("quantity") or raw.get("quantity") or 0.0)
    buy_sell = (raw.get("buySell") or "").upper()
    if "." not in symbol or quantity == 0:
        return {currency: cash_amount} if cash_amount else {}

    base_ccy, _quote_ccy = symbol.split(".", 1)
    base_delta = quantity if buy_sell == "BUY" else -quantity
    effects = {}
    if base_delta:
        effects[base_ccy] = effects.get(base_ccy, 0.0) + base_delta
    if cash_amount:
        effects[currency] = effects.get(currency, 0.0) + cash_amount
    return effects


def _modified_dietz_return(start_value: float, end_value: float, net_flow: float) -> float:
    denominator = start_value + 0.5 * net_flow
    if abs(denominator) < 1e-9:
        return 0.0
    return (end_value - start_value - net_flow) / denominator


def _effective_boundary_flow_base(
    row: Dict[str, object],
    effect: TransactionEffect,
    price_state: Dict[str, Tuple[float, str]],
    base_currency: str,
    fx_state: Dict[Tuple[str, str], float],
) -> float:
    if row.get("asset_class") not in EXCLUDED_EFFECTIVE_ASSET_CLASSES:
        return 0.0
    if effect.quantity_delta == 0:
        return 0.0

    amount = abs(float(row.get("gross_amount") or 0.0)) or abs(float(row.get("cash_amount") or 0.0))
    if amount == 0.0:
        quantity = abs(float(row.get("quantity") or 0.0))
        currency = row.get("currency") or "CNY"
        if row.get("asset_class") == "cash_management":
            amount = quantity * (float(row.get("price") or 1.0) or 1.0)
        elif row.get("asset_class") == "repo":
            amount = quantity
        elif quantity > 0 and row.get("instrument_id") in price_state:
            price, price_currency = price_state[row["instrument_id"]]
            amount = quantity * price
            currency = price_currency or currency
        else:
            amount = quantity
        flow_currency = currency
    else:
        flow_currency = row.get("currency") or "CNY"

    flow_base = amount * _resolve_fx_rate(flow_currency, base_currency, fx_state)
    if effect.quantity_delta > 0:
        return -flow_base
    if effect.quantity_delta < 0:
        return flow_base
    return 0.0


def _effective_bridge_cash_delta(row: Dict[str, object]) -> float:
    if row.get("activity_type") != "history_migration":
        return 0.0
    if row.get("asset_class") not in EXCLUDED_EFFECTIVE_ASSET_CLASSES:
        return 0.0
    return float(row.get("cash_amount") or 0.0)


def _resolve_fx_rate(from_currency: str, to_currency: str, pair_map: Dict[Tuple[str, str], float]) -> float:
    from_currency = (from_currency or to_currency or "").upper()
    to_currency = (to_currency or from_currency or "").upper()
    if not from_currency or not to_currency or from_currency == to_currency:
        return 1.0
    direct = pair_map.get((from_currency, to_currency))
    if direct:
        return direct

    graph = defaultdict(list)
    for (base_currency, quote_currency), rate in pair_map.items():
        if not rate:
            continue
        graph[base_currency].append((quote_currency, rate))
        graph[quote_currency].append((base_currency, 1.0 / rate))

    queue = [(from_currency, 1.0)]
    visited = {from_currency}
    while queue:
        currency, running_rate = queue.pop(0)
        for next_currency, edge_rate in graph.get(currency, []):
            candidate = running_rate * edge_rate
            if next_currency == to_currency:
                return candidate
            if next_currency in visited:
                continue
            visited.add(next_currency)
            queue.append((next_currency, candidate))
    return 1.0


def _seed_opening_quantity(bucket: Dict[str, object], row: Dict[str, object], effect: TransactionEffect) -> None:
    if bucket["quantity"] > 0:
        return
    inferred_before = _infer_pre_transaction_quantity(row, effect)
    if inferred_before <= 0:
        return
    unit_cost = float(row.get("price") or 0.0)
    if unit_cost == 0.0 and effect.quantity_delta != 0:
        unit_cost = abs(float(row.get("gross_amount") or 0.0) / effect.quantity_delta)
    bucket["quantity"] = inferred_before
    bucket["average_cost"] = unit_cost
    bucket["cost_basis_total"] = inferred_before * unit_cost


def _seed_opening_position_state(position_state, row: Dict[str, object], effect: TransactionEffect) -> None:
    instrument_id = row.get("instrument_id")
    if not instrument_id or position_state[instrument_id] > 0:
        return 0.0
    inferred_before = _infer_pre_transaction_quantity(row, effect)
    if inferred_before <= 0:
        return 0.0
    position_state[instrument_id] = inferred_before
    unit_price = float(row.get("price") or 0.0)
    if unit_price == 0.0 and inferred_before:
        gross_amount = float(row.get("gross_amount") or 0.0)
        quantity = abs(float(row.get("quantity") or 0.0))
        if gross_amount and quantity:
            unit_price = gross_amount / quantity
    return inferred_before * unit_price


def _infer_pre_transaction_quantity(row: Dict[str, object], effect: TransactionEffect) -> float:
    position_balance = row.get("position_balance")
    if position_balance in (None, "", 0, 0.0):
        if effect.quantity_delta < 0:
            return abs(effect.quantity_delta)
        return 0.0
    after_quantity = float(position_balance or 0.0)
    if effect.quantity_delta > 0:
        return max(0.0, after_quantity - effect.quantity_delta)
    if effect.quantity_delta < 0:
        return max(0.0, after_quantity + abs(effect.quantity_delta))
    return max(0.0, after_quantity)


def _special_fund_flow_quantity_override(
    row: Dict[str, object],
    pending_asset_state,
    pending_subscriptions_by_instrument,
    pending_redemptions_by_instrument,
    pending_redemption_quantities_by_instrument,
    pending_redemption_fees_by_instrument,
    *,
    account_key: bool = True,
):
    activity_type = row.get("activity_type")
    instrument_id = row.get("instrument_id")
    currency = row.get("currency") or "CNY"
    owner_key = (row.get("account_id"), currency) if account_key else currency
    gross_amount = float(row.get("gross_amount") or 0.0)
    cash_amount = float(row.get("cash_amount") or 0.0)
    quantity = float(row.get("quantity") or 0.0)

    if activity_type == "fund_subscription_fund_out" and quantity == 0 and gross_amount > 0:
        pending_asset_state[owner_key] += gross_amount
        if instrument_id:
            pending_subscriptions_by_instrument[instrument_id] += gross_amount
        return 0.0

    if activity_type == "fund_subscription_confirm" and gross_amount > 0:
        if instrument_id and pending_subscriptions_by_instrument[instrument_id] > 0:
            settled = min(gross_amount, pending_subscriptions_by_instrument[instrument_id])
            pending_asset_state[owner_key] -= settled
            pending_subscriptions_by_instrument[instrument_id] -= settled
        return None

    if activity_type == "fund_redemption_in" and gross_amount > 0:
        if quantity > 0.0 and cash_amount == 0.0:
            pending_redemption_quantities_by_instrument[instrument_id] += quantity
            pending_redemption_fees_by_instrument[instrument_id] += float(row.get("other_fee") or 0.0)
        if cash_amount == 0.0:
            pending_asset_state[owner_key] += gross_amount
            if instrument_id:
                pending_redemptions_by_instrument[instrument_id] += gross_amount
            return None
        if instrument_id and pending_redemptions_by_instrument[instrument_id] >= gross_amount - 0.01:
            pending_asset_state[owner_key] -= gross_amount
            pending_redemptions_by_instrument[instrument_id] -= gross_amount
            return 0.0

    if activity_type == "fund_redemption_in" and quantity > 0.0 and gross_amount == 0.0 and cash_amount == 0.0:
        if instrument_id:
            pending_redemption_quantities_by_instrument[instrument_id] += quantity
            pending_redemption_fees_by_instrument[instrument_id] += float(row.get("other_fee") or 0.0)
        return 0.0

    if activity_type == "fund_redemption_in" and quantity == 0.0 and gross_amount == 0.0 and cash_amount > 0.0:
        if instrument_id and pending_redemption_quantities_by_instrument[instrument_id] > 0.0:
            row["gross_amount"] = abs(cash_amount)
            if pending_redemption_fees_by_instrument[instrument_id] > 0.0:
                row["other_fee"] = float(row.get("other_fee") or 0.0) + pending_redemption_fees_by_instrument[instrument_id]
                pending_redemption_fees_by_instrument[instrument_id] = 0.0
            settled_quantity = pending_redemption_quantities_by_instrument[instrument_id]
            pending_redemption_quantities_by_instrument[instrument_id] = 0.0
            return -settled_quantity
    return None
