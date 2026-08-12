from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, Optional


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = ROOT / "data" / "portfolio.db"
BACKUP_REQUIRED_TABLES = frozenset(
    {
        "accounts",
        "instruments",
        "transactions",
        "cash_flows",
        "price_history",
        "fx_rates",
        "position_snapshots",
        "cash_balances",
        "sync_runs",
    }
)


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS accounts (
    id TEXT PRIMARY KEY,
    broker TEXT NOT NULL,
    account_code TEXT NOT NULL,
    display_name TEXT NOT NULL,
    base_currency TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS instruments (
    id TEXT PRIMARY KEY,
    broker TEXT NOT NULL,
    symbol TEXT NOT NULL,
    name TEXT NOT NULL,
    asset_class TEXT NOT NULL,
    market TEXT NOT NULL,
    currency TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_instruments_broker_symbol
    ON instruments (broker, symbol);

CREATE TABLE IF NOT EXISTS transactions (
    id TEXT PRIMARY KEY,
    broker TEXT NOT NULL,
    account_id TEXT NOT NULL REFERENCES accounts(id),
    instrument_id TEXT REFERENCES instruments(id),
    settle_date TEXT NOT NULL,
    trade_date TEXT NOT NULL,
    activity_type TEXT NOT NULL,
    description TEXT NOT NULL,
    external_flow INTEGER NOT NULL DEFAULT 0,
    quantity REAL,
    price REAL,
    gross_amount REAL,
    cash_amount REAL,
    position_balance REAL,
    cash_balance REAL,
    commission_total REAL,
    commission_net REAL,
    stamp_duty REAL,
    transfer_fee REAL,
    other_fee REAL,
    currency TEXT NOT NULL,
    source_file TEXT,
    raw_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_transactions_trade_date
    ON transactions (trade_date, settle_date);

CREATE TABLE IF NOT EXISTS cash_flows (
    id TEXT PRIMARY KEY,
    transaction_id TEXT NOT NULL REFERENCES transactions(id),
    account_id TEXT NOT NULL REFERENCES accounts(id),
    flow_date TEXT NOT NULL,
    direction TEXT NOT NULL,
    amount REAL NOT NULL,
    currency TEXT NOT NULL,
    description TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS price_history (
    instrument_id TEXT NOT NULL REFERENCES instruments(id),
    price_date TEXT NOT NULL,
    close_price REAL NOT NULL,
    currency TEXT NOT NULL,
    source TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (instrument_id, price_date, source)
);

CREATE TABLE IF NOT EXISTS fx_rates (
    rate_date TEXT NOT NULL,
    base_currency TEXT NOT NULL,
    quote_currency TEXT NOT NULL,
    rate REAL NOT NULL,
    source TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (rate_date, base_currency, quote_currency, source)
);

CREATE TABLE IF NOT EXISTS position_snapshots (
    snapshot_time TEXT NOT NULL,
    account_id TEXT NOT NULL REFERENCES accounts(id),
    instrument_id TEXT NOT NULL REFERENCES instruments(id),
    quantity REAL NOT NULL,
    average_cost REAL,
    market_price REAL,
    market_value REAL,
    unrealized_pnl REAL,
    currency TEXT NOT NULL,
    source TEXT NOT NULL,
    raw_json TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (snapshot_time, account_id, instrument_id, source)
);

CREATE TABLE IF NOT EXISTS cash_balances (
    snapshot_time TEXT NOT NULL,
    account_id TEXT NOT NULL REFERENCES accounts(id),
    currency TEXT NOT NULL,
    amount REAL NOT NULL,
    source TEXT NOT NULL,
    raw_json TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (snapshot_time, account_id, currency, source)
);

CREATE TABLE IF NOT EXISTS sync_runs (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    status TEXT NOT NULL,
    detail_json TEXT NOT NULL DEFAULT '{}'
);
"""


def connect(db_path: Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    return connection


@contextmanager
def open_db(db_path: Path = DEFAULT_DB_PATH) -> Iterator[sqlite3.Connection]:
    connection = connect(db_path)
    try:
        yield connection
        connection.commit()
    finally:
        connection.close()


def init_db(db_path: Path = DEFAULT_DB_PATH) -> None:
    with open_db(db_path) as connection:
        connection.executescript(SCHEMA)


def backup_database(source_path: Path, destination_path: Path) -> Path:
    source_path = Path(source_path)
    destination_path = Path(destination_path)
    if not source_path.is_file():
        raise ValueError(f"Database not found: {source_path}")
    if source_path.resolve() == destination_path.resolve():
        raise ValueError("Backup destination must differ from the source database.")
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(source_path)
    destination = sqlite3.connect(destination_path)
    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()
    return destination_path


def inspect_backup(backup_path: Path) -> Dict[str, object]:
    backup_path = Path(backup_path)
    if not backup_path.is_file():
        raise ValueError("Select a database backup file to restore.")
    with backup_path.open("rb") as stream:
        if stream.read(16) != b"SQLite format 3\x00":
            raise ValueError("The selected file is not a SQLite database backup.")

    connection = sqlite3.connect(backup_path)
    try:
        connection.execute("PRAGMA trusted_schema = OFF")
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise ValueError(f"Database integrity check failed: {integrity}")

        schema_rows = connection.execute(
            "SELECT type, name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
        ).fetchall()
        tables = {name for object_type, name in schema_rows if object_type == "table"}
        missing_tables = sorted(BACKUP_REQUIRED_TABLES - tables)
        if missing_tables:
            raise ValueError(f"Backup is missing required tables: {', '.join(missing_tables)}")

        unsupported_objects = sorted(
            name for object_type, name in schema_rows if object_type in {"trigger", "view"}
        )
        if unsupported_objects:
            raise ValueError("Backup contains unsupported database objects.")

        foreign_key_errors = connection.execute("PRAGMA foreign_key_check").fetchall()
        if foreign_key_errors:
            raise ValueError("Backup contains broken database relationships.")

        return {
            "transactions": connection.execute("SELECT COUNT(1) FROM transactions").fetchone()[0],
            "accounts": connection.execute("SELECT COUNT(1) FROM accounts").fetchone()[0],
            "instruments": connection.execute("SELECT COUNT(1) FROM instruments").fetchone()[0],
        }
    except sqlite3.DatabaseError as exc:
        raise ValueError(f"Unable to read database backup: {exc}") from exc
    finally:
        connection.close()


def restore_database(source_path: Path, target_path: Path, rollback_path: Path) -> Dict[str, object]:
    inspection = inspect_backup(source_path)
    backup_database(target_path, rollback_path)
    try:
        backup_database(source_path, target_path)
        init_db(target_path)
        restored = inspect_backup(target_path)
    except Exception:
        backup_database(rollback_path, target_path)
        raise
    if restored != inspection:
        backup_database(rollback_path, target_path)
        raise ValueError("Restored database did not match the selected backup.")
    return restored


def upsert_account(connection: sqlite3.Connection, payload: Dict[str, object]) -> None:
    connection.execute(
        """
        INSERT INTO accounts (id, broker, account_code, display_name, base_currency, metadata_json)
        VALUES (:id, :broker, :account_code, :display_name, :base_currency, :metadata_json)
        ON CONFLICT(id) DO UPDATE SET
            broker = excluded.broker,
            account_code = excluded.account_code,
            display_name = excluded.display_name,
            base_currency = excluded.base_currency,
            metadata_json = excluded.metadata_json,
            updated_at = CURRENT_TIMESTAMP
        """,
        {
            **payload,
            "metadata_json": json.dumps(payload.get("metadata_json", {}), ensure_ascii=False),
        },
    )


def upsert_instrument(connection: sqlite3.Connection, payload: Dict[str, object]) -> None:
    connection.execute(
        """
        INSERT INTO instruments (id, broker, symbol, name, asset_class, market, currency, metadata_json)
        VALUES (:id, :broker, :symbol, :name, :asset_class, :market, :currency, :metadata_json)
        ON CONFLICT(id) DO UPDATE SET
            broker = excluded.broker,
            symbol = excluded.symbol,
            name = excluded.name,
            asset_class = excluded.asset_class,
            market = excluded.market,
            currency = excluded.currency,
            metadata_json = excluded.metadata_json,
            updated_at = CURRENT_TIMESTAMP
        """,
        {
            **payload,
            "metadata_json": json.dumps(payload.get("metadata_json", {}), ensure_ascii=False),
        },
    )


def insert_transaction(connection: sqlite3.Connection, payload: Dict[str, object]) -> None:
    connection.execute(
        """
        INSERT OR REPLACE INTO transactions (
            id, broker, account_id, instrument_id, settle_date, trade_date, activity_type,
            description, external_flow, quantity, price, gross_amount, cash_amount,
            position_balance, cash_balance, commission_total, commission_net, stamp_duty,
            transfer_fee, other_fee, currency, source_file, raw_json
        ) VALUES (
            :id, :broker, :account_id, :instrument_id, :settle_date, :trade_date, :activity_type,
            :description, :external_flow, :quantity, :price, :gross_amount, :cash_amount,
            :position_balance, :cash_balance, :commission_total, :commission_net, :stamp_duty,
            :transfer_fee, :other_fee, :currency, :source_file, :raw_json
        )
        """,
        {
            **payload,
            "raw_json": json.dumps(payload.get("raw_json", {}), ensure_ascii=False),
        },
    )


def insert_cash_flow(connection: sqlite3.Connection, payload: Dict[str, object]) -> None:
    connection.execute(
        """
        INSERT OR REPLACE INTO cash_flows (
            id, transaction_id, account_id, flow_date, direction, amount, currency, description
        ) VALUES (
            :id, :transaction_id, :account_id, :flow_date, :direction, :amount, :currency, :description
        )
        """,
        payload,
    )


def upsert_price(connection: sqlite3.Connection, payload: Dict[str, object]) -> None:
    connection.execute(
        """
        INSERT INTO price_history (instrument_id, price_date, close_price, currency, source)
        VALUES (:instrument_id, :price_date, :close_price, :currency, :source)
        ON CONFLICT(instrument_id, price_date, source) DO UPDATE SET
            close_price = excluded.close_price,
            currency = excluded.currency,
            updated_at = CURRENT_TIMESTAMP
        """,
        payload,
    )


def upsert_fx_rate(connection: sqlite3.Connection, payload: Dict[str, object]) -> None:
    connection.execute(
        """
        INSERT INTO fx_rates (rate_date, base_currency, quote_currency, rate, source)
        VALUES (:rate_date, :base_currency, :quote_currency, :rate, :source)
        ON CONFLICT(rate_date, base_currency, quote_currency, source) DO UPDATE SET
            rate = excluded.rate,
            updated_at = CURRENT_TIMESTAMP
        """,
        payload,
    )


def replace_position_snapshots(
    connection: sqlite3.Connection,
    snapshot_time: str,
    source: str,
    rows: Iterable[Dict[str, object]],
) -> None:
    connection.execute(
        "DELETE FROM position_snapshots WHERE snapshot_time = ? AND source = ?",
        (snapshot_time, source),
    )
    connection.executemany(
        """
        INSERT INTO position_snapshots (
            snapshot_time, account_id, instrument_id, quantity, average_cost,
            market_price, market_value, unrealized_pnl, currency, source, raw_json
        ) VALUES (
            :snapshot_time, :account_id, :instrument_id, :quantity, :average_cost,
            :market_price, :market_value, :unrealized_pnl, :currency, :source, :raw_json
        )
        """,
        [
            {
                **row,
                "snapshot_time": snapshot_time,
                "source": source,
                "raw_json": json.dumps(row.get("raw_json", {}), ensure_ascii=False),
            }
            for row in rows
        ],
    )


def replace_cash_balances(
    connection: sqlite3.Connection,
    snapshot_time: str,
    source: str,
    rows: Iterable[Dict[str, object]],
) -> None:
    connection.execute(
        "DELETE FROM cash_balances WHERE snapshot_time = ? AND source = ?",
        (snapshot_time, source),
    )
    connection.executemany(
        """
        INSERT INTO cash_balances (
            snapshot_time, account_id, currency, amount, source, raw_json
        ) VALUES (
            :snapshot_time, :account_id, :currency, :amount, :source, :raw_json
        )
        """,
        [
            {
                **row,
                "snapshot_time": snapshot_time,
                "source": source,
                "raw_json": json.dumps(row.get("raw_json", {}), ensure_ascii=False),
            }
            for row in rows
        ],
    )


def insert_sync_run(connection: sqlite3.Connection, payload: Dict[str, object]) -> None:
    connection.execute(
        """
        INSERT OR REPLACE INTO sync_runs (id, source, started_at, completed_at, status, detail_json)
        VALUES (:id, :source, :started_at, :completed_at, :status, :detail_json)
        """,
        {
            **payload,
            "detail_json": json.dumps(payload.get("detail_json", {}), ensure_ascii=False),
        },
    )


def fetch_all(connection: sqlite3.Connection, query: str, params: Optional[Iterable[object]] = None) -> List[sqlite3.Row]:
    cursor = connection.execute(query, tuple(params or ()))
    return list(cursor.fetchall())


def fetch_one(connection: sqlite3.Connection, query: str, params: Optional[Iterable[object]] = None) -> Optional[sqlite3.Row]:
    cursor = connection.execute(query, tuple(params or ()))
    return cursor.fetchone()

