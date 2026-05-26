from __future__ import annotations

import re
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from pypdf import PdfReader


DATE_RE = re.compile(r"^\d{8}$")
HEADER_MARKER = "交收日期"


COLUMN_ANCHORS = [
    ("settle_date", 18.75),
    ("account_code", 67.92),
    ("business", 117.08),
    ("symbol", 179.90),
    ("name", 229.07),
    ("price", 308.00),
    ("quantity", 352.55),
    ("gross_amount", 397.09),
    ("position_balance", 446.26),
    ("cash_amount", 500.89),
    ("cash_balance", 555.52),
    ("commission_total", 610.35),
    ("commission_net", 651.32),
    ("stamp_duty", 692.29),
    ("transfer_fee", 733.26),
    ("currency", 753.50),
    ("other_fee", 807.01),
]


@dataclass
class ParsedGtjaRow:
    settle_date: str
    account_code: str
    business: str
    symbol: str
    name: str
    price: float
    quantity: float
    gross_amount: float
    position_balance: float
    cash_amount: float
    cash_balance: float
    commission_total: float
    commission_net: float
    stamp_duty: float
    transfer_fee: float
    currency: str
    other_fee: float
    activity_type: str
    external_flow: bool
    asset_class: str
    market: str
    source_file: str

    @property
    def fees_total(self) -> float:
        return (
            self.commission_total
            + self.commission_net
            + self.stamp_duty
            + self.transfer_fee
            + self.other_fee
        )

    def to_dict(self) -> Dict[str, object]:
        payload = asdict(self)
        payload["fees_total"] = self.fees_total
        return payload


def parse_gtja_statement(pdf_path: Path) -> List[ParsedGtjaRow]:
    reader = PdfReader(str(pdf_path))
    rows: List[ParsedGtjaRow] = []
    for page in reader.pages:
        fragments = _extract_fragments(page)
        row_anchors = sorted(
            {
                round(y, 1)
                for x, y, text in fragments
                if x < 40 and DATE_RE.match(text)
            },
            reverse=True,
        )
        for anchor in row_anchors:
            row_fragments = [(x, y, text) for x, y, text in fragments if abs(y - anchor) <= 7]
            parsed = _parse_row(row_fragments, str(pdf_path))
            if parsed:
                rows.append(parsed)
    return _normalize_fund_confirmation_rows(rows)


def import_gtja_statement(connection, pdf_path: Path) -> Dict[str, object]:
    from . import db

    rows = parse_gtja_statement(pdf_path)
    rows = _dedupe_parsed_rows(rows)
    if rows:
        _delete_gtja_rows_in_date_range(
            connection,
            _iso_date(min(row.settle_date for row in rows)),
            _iso_date(max(row.settle_date for row in rows)),
        )
    account_id = "gtja:48530"
    _delete_gtja_rows_by_fingerprint(connection, {_row_fingerprint(row) for row in rows})
    db.upsert_account(
        connection,
        {
            "id": account_id,
            "broker": "gtja",
            "account_code": "48530",
            "display_name": "国泰海通 48530",
            "base_currency": "CNY",
            "metadata_json": {"source_file": str(pdf_path)},
        },
    )

    inserted = 0
    external_flow_total = 0.0
    for row in rows:
        instrument_id = None
        if row.symbol:
            instrument_id = f"gtja:{row.symbol}"
            implied_price = row.price
            if implied_price <= 0 and row.quantity > 0 and row.gross_amount > 0 and row.asset_class == "cash_management":
                implied_price = row.gross_amount / row.quantity
            db.upsert_instrument(
                connection,
                {
                    "id": instrument_id,
                    "broker": "gtja",
                    "symbol": row.symbol,
                    "name": row.name or row.symbol,
                    "asset_class": row.asset_class,
                    "market": row.market,
                    "currency": row.currency or "CNY",
                    "metadata_json": {"source_business": row.business},
                },
            )
            if implied_price > 0:
                db.upsert_price(
                    connection,
                    {
                        "instrument_id": instrument_id,
                        "price_date": _iso_date(row.trade_date if hasattr(row, "trade_date") else row.settle_date),
                        "close_price": implied_price,
                        "currency": row.currency or "CNY",
                        "source": "gtja_transaction",
                    },
                )

        transaction_id = _transaction_id_for_row(row)
        db.insert_transaction(
            connection,
            {
                "id": transaction_id,
                "broker": "gtja",
                "account_id": account_id,
                "instrument_id": instrument_id,
                "settle_date": _iso_date(row.settle_date),
                "trade_date": _iso_date(row.settle_date),
                "activity_type": row.activity_type,
                "description": row.business,
                "external_flow": int(row.external_flow),
                "quantity": _nullable(row.quantity),
                "price": _nullable(row.price),
                "gross_amount": _nullable(row.gross_amount),
                "cash_amount": _nullable(row.cash_amount),
                "position_balance": _nullable(row.position_balance),
                "cash_balance": _nullable(row.cash_balance),
                "commission_total": _nullable(row.commission_total),
                "commission_net": _nullable(row.commission_net),
                "stamp_duty": _nullable(row.stamp_duty),
                "transfer_fee": _nullable(row.transfer_fee),
                "other_fee": _nullable(row.other_fee),
                "currency": row.currency or "CNY",
                "source_file": row.source_file,
                "raw_json": row.to_dict(),
            },
        )
        inserted += 1
        if row.external_flow and row.cash_amount:
            direction = "in" if row.cash_amount > 0 else "out"
            db.insert_cash_flow(
                connection,
                {
                    "id": f"cf:{transaction_id}",
                    "transaction_id": transaction_id,
                    "account_id": account_id,
                    "flow_date": _iso_date(row.settle_date),
                    "direction": direction,
                    "amount": abs(row.cash_amount),
                    "currency": row.currency or "CNY",
                    "description": row.business,
                },
            )
            external_flow_total += row.cash_amount

    return {
        "account_id": account_id,
        "rows": inserted,
        "external_flow_total": round(external_flow_total, 2),
    }


def _dedupe_parsed_rows(rows: List[ParsedGtjaRow]) -> List[ParsedGtjaRow]:
    deduped: List[ParsedGtjaRow] = []
    seen = set()
    for row in rows:
        fingerprint = _row_fingerprint(row)
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        deduped.append(row)
    return deduped


def _normalize_fund_confirmation_rows(rows: List[ParsedGtjaRow]) -> List[ParsedGtjaRow]:
    grouped: Dict[tuple, List[ParsedGtjaRow]] = {}
    for row in rows:
        grouped.setdefault((row.settle_date, row.symbol, row.activity_type), []).append(row)

    for (_settle_date, _symbol, activity_type), group in grouped.items():
        if activity_type != "fund_subscription_confirm":
            continue
        quantity_rows = [row for row in group if row.quantity > 0 and row.price == 0 and row.gross_amount == 0]
        cash_rows = [row for row in group if row.quantity == 0 and row.cash_amount < 0]
        for quantity_row, cash_row in zip(quantity_rows, cash_rows):
            implied_gross = abs(cash_row.cash_amount)
            if quantity_row.quantity > 0 and implied_gross > 0:
                quantity_row.gross_amount = implied_gross
                quantity_row.price = implied_gross / quantity_row.quantity
                cash_row.activity_type = "fund_subscription_fund_out"
    return rows


def _delete_gtja_rows_by_fingerprint(connection, fingerprints) -> None:
    from . import db

    if not fingerprints:
        return
    rows = db.fetch_all(
        connection,
        """
        SELECT
            t.id,
            t.trade_date,
            a.account_code,
            t.description,
            i.symbol,
            i.name,
            t.price,
            t.quantity,
            t.gross_amount,
            t.position_balance,
            t.cash_amount,
            t.cash_balance,
            t.commission_total,
            t.commission_net,
            t.stamp_duty,
            t.transfer_fee,
            t.currency,
            t.other_fee,
            t.activity_type,
            i.asset_class,
            i.market
        FROM transactions t
        JOIN accounts a ON a.id = t.account_id
        LEFT JOIN instruments i ON i.id = t.instrument_id
        WHERE t.broker = 'gtja'
        """,
    )
    to_delete = []
    for db_row in rows:
        if _db_row_fingerprint(db_row) in fingerprints:
            to_delete.append((db_row["id"],))
    if not to_delete:
        return
    connection.executemany("DELETE FROM cash_flows WHERE transaction_id = ?", to_delete)
    connection.executemany("DELETE FROM transactions WHERE id = ?", to_delete)


def _delete_gtja_rows_in_date_range(connection, start_date: str, end_date: str) -> None:
    from . import db

    rows = db.fetch_all(
        connection,
        """
        SELECT id
        FROM transactions
        WHERE broker = 'gtja'
          AND trade_date BETWEEN ? AND ?
        """,
        [start_date, end_date],
    )
    if not rows:
        return
    ids = [(row["id"],) for row in rows]
    connection.executemany("DELETE FROM cash_flows WHERE transaction_id = ?", ids)
    connection.executemany("DELETE FROM transactions WHERE id = ?", ids)
    connection.execute(
        """
        DELETE FROM price_history
        WHERE source = 'gtja_transaction'
          AND price_date BETWEEN ? AND ?
        """,
        [start_date, end_date],
    )


def _transaction_id_for_row(row: ParsedGtjaRow) -> str:
    return f"gtja:{uuid.uuid5(uuid.NAMESPACE_URL, str(_row_fingerprint(row)))}"


def _row_fingerprint(row: ParsedGtjaRow):
    return (
        _iso_date(row.settle_date),
        row.account_code,
        row.business,
        row.symbol,
        row.name,
        round(row.price, 6),
        round(row.quantity, 6),
        round(row.gross_amount, 6),
        round(row.position_balance, 6),
        round(row.cash_amount, 6),
        round(row.cash_balance, 6),
        round(row.commission_total, 6),
        round(row.commission_net, 6),
        round(row.stamp_duty, 6),
        round(row.transfer_fee, 6),
        row.currency or "CNY",
        round(row.other_fee, 6),
        row.activity_type,
        row.asset_class,
        row.market,
    )


def _db_row_fingerprint(row):
    return (
        row["trade_date"],
        row["account_code"] or "",
        row["description"] or "",
        row["symbol"] or "",
        row["name"] or "",
        round(float(row["price"] or 0.0), 6),
        round(float(row["quantity"] or 0.0), 6),
        round(float(row["gross_amount"] or 0.0), 6),
        round(float(row["position_balance"] or 0.0), 6),
        round(float(row["cash_amount"] or 0.0), 6),
        round(float(row["cash_balance"] or 0.0), 6),
        round(float(row["commission_total"] or 0.0), 6),
        round(float(row["commission_net"] or 0.0), 6),
        round(float(row["stamp_duty"] or 0.0), 6),
        round(float(row["transfer_fee"] or 0.0), 6),
        row["currency"] or "CNY",
        round(float(row["other_fee"] or 0.0), 6),
        row["activity_type"] or "",
        row["asset_class"] or "other",
        row["market"] or "UNKNOWN",
    )


def _extract_fragments(page) -> List[tuple]:
    fragments: List[tuple] = []

    def visitor(text, _cm, tm, _font_dict, _font_size):
        cleaned = _clean_fragment(text)
        if not cleaned:
            return
        fragments.append((round(tm[4], 2), round(tm[5], 2), cleaned))

    page.extract_text(visitor_text=visitor)
    return fragments


def _parse_row(fragments: Iterable[tuple], source_file: str) -> Optional[ParsedGtjaRow]:
    bucketed: Dict[str, List[tuple]] = {key: [] for key, _anchor in COLUMN_ANCHORS}
    for x, y, text in fragments:
        column = _column_for_x(x)
        if column:
            bucketed[column].append((x, y, text))

    settle_date = _merge_cell(bucketed["settle_date"])
    if not DATE_RE.match(settle_date):
        return None

    business = _merge_cell(bucketed["business"])
    symbol = _merge_cell(bucketed["symbol"])
    name = _merge_cell(bucketed["name"])
    price = _parse_float(_merge_cell(bucketed["price"]))
    quantity = _parse_float(_merge_cell(bucketed["quantity"]))
    gross_amount = _parse_float(_merge_cell(bucketed["gross_amount"]))
    position_balance = _parse_float(_merge_cell(bucketed["position_balance"]))
    cash_amount = _parse_float(_merge_cell(bucketed["cash_amount"]))
    cash_balance = _parse_float(_merge_cell(bucketed["cash_balance"]))
    commission_total = _parse_float(_merge_cell(bucketed["commission_total"]))
    commission_net = _parse_float(_merge_cell(bucketed["commission_net"]))
    stamp_duty = _parse_float(_merge_cell(bucketed["stamp_duty"]))
    transfer_fee = _parse_float(_merge_cell(bucketed["transfer_fee"]))
    currency = _normalize_currency(_merge_cell(bucketed["currency"]) or "CNY")
    other_fee = _parse_float(_merge_cell(bucketed["other_fee"]))
    account_code = _merge_cell(bucketed["account_code"])

    activity_type, external_flow = _activity_type_for_business(business)
    asset_class = _classify_asset_class(business, symbol, name)
    market = _classify_market(symbol, currency)

    return ParsedGtjaRow(
        settle_date=settle_date,
        account_code=account_code,
        business=business,
        symbol=symbol,
        name=name,
        price=price,
        quantity=quantity,
        gross_amount=gross_amount,
        position_balance=position_balance,
        cash_amount=cash_amount,
        cash_balance=cash_balance,
        commission_total=commission_total,
        commission_net=commission_net,
        stamp_duty=stamp_duty,
        transfer_fee=transfer_fee,
        currency=currency,
        other_fee=other_fee,
        activity_type=activity_type,
        external_flow=external_flow,
        asset_class=asset_class,
        market=market,
        source_file=source_file,
    )


def _column_for_x(x: float) -> Optional[str]:
    for index, (column, anchor) in enumerate(COLUMN_ANCHORS):
        left = float("-inf") if index == 0 else (COLUMN_ANCHORS[index - 1][1] + anchor) / 2
        right = float("inf") if index == len(COLUMN_ANCHORS) - 1 else (anchor + COLUMN_ANCHORS[index + 1][1]) / 2
        if left <= x < right:
            return column
    return None


def _merge_cell(items: List[tuple]) -> str:
    if not items:
        return ""
    ordered = sorted(items, key=lambda item: (-item[1], item[0]))
    return "".join(piece for _x, _y, piece in ordered)


def _clean_fragment(text: str) -> str:
    cleaned = text.replace("\n", "").replace("\r", "").strip()
    cleaned = cleaned.replace(" ", "")
    return cleaned


def _parse_float(raw: str) -> float:
    if not raw:
        return 0.0
    try:
        return float(raw.replace(",", ""))
    except ValueError:
        return 0.0


def _activity_type_for_business(business: str) -> tuple[str, bool]:
    mapping = [
        ("银行转证券", ("bank_transfer_in", True)),
        ("证券转银行", ("bank_transfer_out", True)),
        ("历史数据迁移", ("history_migration", False)),
        ("证券买入", ("security_buy", False)),
        ("证券卖出", ("security_sell", False)),
        ("普通买入", ("security_buy", False)),
        ("普通卖出", ("security_sell", False)),
        ("基金申购拨出", ("fund_subscription_fund_out", False)),
        ("基金申购确认", ("fund_subscription_confirm", False)),
        ("基金赎回拨入", ("fund_redemption_in", False)),
        ("基金T+0转出", ("fund_redemption_in", False)),
        ("基金非交易过户出", ("position_adjustment_out", False)),
        ("融券回购", ("repo_open", False)),
        ("报价融券回购", ("repo_open", False)),
        ("融券购回", ("repo_close", False)),
        ("报价融券购回", ("repo_close", False)),
        ("深圳A股网上新股配号", ("ipo_lottery", False)),
        ("股息个税征收", ("dividend_tax", False)),
        ("红利入账", ("dividend", False)),
        ("分红入账", ("dividend", False)),
        ("现金红利", ("dividend", False)),
        ("红利税", ("dividend_tax", False)),
        ("股息税", ("dividend_tax", False)),
        ("利息归本", ("interest_credit", False)),
        ("基金红股", ("position_adjustment", False)),
        ("送股", ("position_adjustment", False)),
        ("转增", ("position_adjustment", False)),
        ("拆分", ("position_adjustment", False)),
        ("基金强行赎回拨入", ("position_adjustment_out", False)),
        ("基金份额强减", ("position_adjustment_out", False)),
        ("并股", ("position_adjustment_out", False)),
        ("合股", ("position_adjustment_out", False)),
    ]
    for pattern, result in mapping:
        if pattern in business:
            return result
    slug = re.sub(r"[^a-z0-9]+", "_", business.lower()).strip("_") or "unknown"
    return slug, False


def _classify_asset_class(business: str, symbol: str, name: str) -> str:
    if "银行转证券" in business or "证券转银行" in business:
        return "cash_transfer"
    if symbol.startswith(("85",)):
        return "cash_management"
    if "现金管家" in name:
        return "cash_management"
    if "基金" in business:
        return "fund"
    if "回购" in business or symbol in {"131810", "131811", "132818", "204001", "204002", "204003", "132005014242"}:
        return "repo"
    if "国债" in name:
        return "bond_fund"
    if any(keyword in name for keyword in ("基金", "混合", "债券", "联接", "LOF")):
        return "fund"
    if symbol.startswith(("5", "1")):
        return "etf"
    if symbol.startswith(("95",)):
        return "fund"
    if symbol.startswith(("0", "3", "6")):
        return "equity"
    if "利息" in business:
        return "cash"
    return "other"


def _classify_market(symbol: str, currency: str) -> str:
    if currency == "USD":
        return "US"
    if currency == "HKD":
        return "HK"
    if symbol.isdigit():
        return "CN"
    return "UNKNOWN"


def _iso_date(raw: str) -> str:
    if len(raw) == 8 and raw.isdigit():
        return f"{raw[:4]}-{raw[4:6]}-{raw[6:]}"
    return raw


def _nullable(value: float) -> Optional[float]:
    return None if value == 0 else value


def _normalize_currency(value: str) -> str:
    mapping = {
        "人民币": "CNY",
        "美元": "USD",
        "港币": "HKD",
    }
    return mapping.get(value, value or "CNY")
