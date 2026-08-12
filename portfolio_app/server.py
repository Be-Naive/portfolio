from __future__ import annotations

import json
from email.parser import BytesParser
from email.policy import default as email_policy
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Optional
from urllib.parse import parse_qs

from jinja2 import Environment, FileSystemLoader, select_autoescape

from . import analytics, db, gtja_pdf, ibkr_client, market_data


ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_DIR = ROOT / "portfolio_app" / "templates"
STATIC_DIR = ROOT / "portfolio_app" / "static"
DEFAULT_SAMPLE_PDF = Path(
    "/Users/bytedance/Library/Containers/com.tencent.xinWeChat/Data/Documents/"
    "xwechat_files/wxid_5306923069012_3823/msg/file/2026-03/pdf_viewer_1774344494948.pdf"
)
DEFAULT_SAMPLE_XML = Path("/Users/bytedance/Downloads/portfolio_performance (1).xml")
AUTO_MARKET_REFRESH_INTERVAL = timedelta(minutes=15)
MARKET_SYNC_SOURCES = ("market_data_auto", "market_data_manual")
MAX_BACKUP_UPLOAD_BYTES = 256 * 1024 * 1024


class PortfolioApplication:
    def __init__(self, db_path: Path = db.DEFAULT_DB_PATH):
        self.db_path = db_path
        db.init_db(db_path)
        self.templates = Environment(
            loader=FileSystemLoader(str(TEMPLATE_DIR)),
            autoescape=select_autoescape(["html", "xml"]),
        )

    def __call__(self, environ, start_response):
        method = environ["REQUEST_METHOD"]
        path = environ.get("PATH_INFO", "/")
        try:
            if method == "GET" and path == "/":
                return self._index(environ, start_response)
            if method == "GET" and path == "/static/style.css":
                return self._static_css(start_response)
            if method == "GET" and path == "/api/benchmark-series":
                return self._api_benchmark_series(environ, start_response)
            if method == "GET" and path == "/actions/export-database":
                return self._export_database(start_response)
            if method == "POST" and path == "/actions/import-gtja":
                return self._import_gtja(environ, start_response)
            if method == "POST" and path == "/actions/sync-ibkr":
                return self._sync_ibkr(environ, start_response)
            if method == "POST" and path == "/actions/import-ibkr-xml":
                return self._import_ibkr_xml(environ, start_response)
            if method == "POST" and path == "/actions/refresh-market-data":
                return self._refresh_market_data(environ, start_response)
            if method == "POST" and path == "/actions/rebalance":
                return self._rebalance(environ, start_response)
            if method == "POST" and path == "/actions/import-database":
                return self._import_database(environ, start_response)
            if method == "GET" and path == "/api/dashboard":
                return self._api_dashboard(start_response)
            return self._respond(start_response, "404 Not Found", "Not found", "text/plain")
        except Exception as exc:  # pragma: no cover - surfaced in UI
            return self._render_dashboard(
                start_response,
                status="500 Internal Server Error",
                error=str(exc),
            )

    def _index(self, environ, start_response):
        query = parse_qs(environ.get("QUERY_STRING", ""))
        return self._render_dashboard(
            start_response,
            message=query.get("message", [""])[0] or None,
            error=query.get("error", [""])[0] or None,
        )

    def _api_dashboard(self, start_response):
        with db.open_db(self.db_path) as connection:
            payload = analytics.build_dashboard(connection)
        return self._respond(start_response, "200 OK", json.dumps(payload, ensure_ascii=False), "application/json")

    def _api_benchmark_series(self, environ, start_response):
        query = parse_qs(environ.get("QUERY_STRING", ""))
        instrument_id = (query.get("id", [""])[0] or "").strip()
        if not instrument_id:
            return self._respond(
                start_response,
                "400 Bad Request",
                json.dumps({"error": "Benchmark instrument id is required."}, ensure_ascii=False),
                "application/json",
            )
        with db.open_db(self.db_path) as connection:
            dashboard = analytics.build_dashboard(connection)
        payload = dashboard["benchmark_series"].get(instrument_id)
        if not payload:
            return self._respond(
                start_response,
                "404 Not Found",
                json.dumps({"error": f"No benchmark series found for {instrument_id}."}, ensure_ascii=False),
                "application/json",
            )
        return self._respond(start_response, "200 OK", json.dumps(payload, ensure_ascii=False), "application/json")

    def _static_css(self, start_response):
        css = STATIC_DIR.joinpath("style.css").read_text(encoding="utf-8")
        return self._respond(start_response, "200 OK", css, "text/css; charset=utf-8")

    def _export_database(self, start_response):
        export_name = f"portfolio-backup-{datetime.now().strftime('%Y%m%d-%H%M%S')}.db"
        with tempfile.TemporaryDirectory() as temp_dir:
            export_path = Path(temp_dir) / export_name
            db.backup_database(self.db_path, export_path)
            payload = export_path.read_bytes()
        return self._respond_bytes(
            start_response,
            "200 OK",
            payload,
            "application/vnd.sqlite3",
            [
                ("Content-Disposition", f'attachment; filename="{export_name}"'),
                ("Cache-Control", "no-store"),
                ("X-Content-Type-Options", "nosniff"),
            ],
        )

    def _import_database(self, environ, start_response):
        try:
            fields, files = self._parse_multipart_form(environ)
            if fields.get("confirm_restore") != "1":
                raise ValueError("Confirm that the current ledger may be replaced before restoring.")
            uploaded = files.get("database_file")
            if not uploaded:
                raise ValueError("Select a portfolio database backup to restore.")
            filename, payload = uploaded
            if not payload:
                raise ValueError("The selected database backup is empty.")

            rollback_dir = self.db_path.parent / "backups" / "database"
            rollback_name = (
                f"portfolio-before-dashboard-restore-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
                f"-{uuid.uuid4().hex[:8]}.db"
            )
            rollback_path = rollback_dir / rollback_name
            with tempfile.TemporaryDirectory() as temp_dir:
                upload_path = Path(temp_dir) / "portfolio-upload.db"
                upload_path.write_bytes(payload)
                restored = db.restore_database(upload_path, self.db_path, rollback_path)
        except ValueError as exc:
            return self._render_dashboard(
                start_response,
                status="400 Bad Request",
                error=str(exc),
            )

        return self._render_dashboard(
            start_response,
            message=(
                f"Restored {restored['transactions']} transactions from {filename}. "
                f"Previous ledger saved as {rollback_name}."
            ),
        )

    def _import_gtja(self, environ, start_response):
        form = self._parse_form(environ)
        pdf_path = Path(form.get("pdf_path", str(DEFAULT_SAMPLE_PDF)))
        if not pdf_path.exists():
            return self._render_dashboard(
                start_response,
                status="400 Bad Request",
                error=f"PDF not found: {pdf_path}",
            )
        with db.open_db(self.db_path) as connection:
            result = gtja_pdf.import_gtja_statement(connection, pdf_path)
        return self._render_dashboard(
            start_response,
            message=f"Imported {result['rows']} statement rows from {pdf_path.name}. External cash flow total: {result['external_flow_total']:.2f} CNY.",
        )

    def _sync_ibkr(self, environ, start_response):
        form = self._parse_form(environ)
        service_url = form.get("service_url", ibkr_client.FLEX_SERVICE_URL).strip() or ibkr_client.FLEX_SERVICE_URL
        token = form.get("token", "").strip()
        query_id = form.get("query_id", "").strip()
        if not token or not query_id:
            return self._render_dashboard(
                start_response,
                status="400 Bad Request",
                error="IBKR Flex sync needs both a Flex Web Service token and a query id.",
            )
        run_id = str(uuid.uuid4())
        with db.open_db(self.db_path) as connection:
            db.insert_sync_run(
                connection,
                {
                    "id": run_id,
                    "source": "ibkr",
                    "started_at": _timestamp(),
                    "completed_at": None,
                    "status": "running",
                    "detail_json": {"service_url": service_url, "query_id": query_id},
                },
            )
        try:
            client = ibkr_client.IbkrClient(token=token, query_id=query_id, service_url=service_url)
            with db.open_db(self.db_path) as connection:
                result = client.sync(connection)
                db.insert_sync_run(
                    connection,
                    {
                        "id": run_id,
                        "source": "ibkr",
                        "started_at": _timestamp(),
                        "completed_at": _timestamp(),
                        "status": "success",
                        "detail_json": result.__dict__,
                    },
                )
            return self._render_dashboard(
                start_response,
                message=(
                    f"IBKR Flex sync complete. {result.transactions} transactions, {result.positions} positions, "
                    f"{result.cash_balances} cash balances."
                ),
            )
        except Exception as exc:
            with db.open_db(self.db_path) as connection:
                db.insert_sync_run(
                    connection,
                    {
                        "id": run_id,
                        "source": "ibkr",
                        "started_at": _timestamp(),
                        "completed_at": _timestamp(),
                        "status": "failed",
                        "detail_json": {"service_url": service_url, "query_id": query_id, "error": str(exc)},
                    },
                )
            return self._render_dashboard(start_response, error=str(exc))

    def _import_ibkr_xml(self, environ, start_response):
        form = self._parse_form(environ)
        xml_path = Path(form.get("xml_path", str(DEFAULT_SAMPLE_XML))).expanduser()
        if not xml_path.exists():
            return self._render_dashboard(
                start_response,
                status="400 Bad Request",
                error=f"XML not found: {xml_path}",
            )
        xml_text = xml_path.read_text(encoding="utf-8")
        with db.open_db(self.db_path) as connection:
            result = ibkr_client.import_flex_statement_file(
                connection,
                xml_text,
                source_label=f"IBKR Flex XML {xml_path.name}",
            )
        return self._render_dashboard(
            start_response,
            message=(
                f"Imported {xml_path.name}. Added or refreshed {result.transactions} IBKR rows "
                f"for {result.statement_from} to {result.statement_to}."
            ),
        )

    def _refresh_market_data(self, environ, start_response):
        with db.open_db(self.db_path) as connection:
            run_id = str(uuid.uuid4())
            started_at = _timestamp()
            db.insert_sync_run(
                connection,
                {
                    "id": run_id,
                    "source": "market_data_manual",
                    "started_at": started_at,
                    "completed_at": None,
                    "status": "running",
                    "detail_json": {"mode": "full"},
                },
            )
            try:
                result = market_data.refresh_market_data(connection)
                db.insert_sync_run(
                    connection,
                    {
                        "id": run_id,
                        "source": "market_data_manual",
                        "started_at": started_at,
                        "completed_at": _timestamp(),
                        "status": "success",
                        "detail_json": result,
                    },
                )
            except Exception as exc:
                db.insert_sync_run(
                    connection,
                    {
                        "id": run_id,
                        "source": "market_data_manual",
                        "started_at": started_at,
                        "completed_at": _timestamp(),
                        "status": "failed",
                        "detail_json": {"mode": "full", "error": str(exc)},
                    },
                )
                raise
        return self._render_dashboard(
            start_response,
            message=(
                f"Historical market data refreshed. {result['prices']} price rows and "
                f"{result['fx_rates']} FX rows upserted."
            ),
        )

    def _rebalance(self, environ, start_response):
        form_values = self._parse_form_values(environ)
        raw_targets = form_values.get("targets", [""])[0]
        selected_labels = [value for value in form_values.get("selected_rebalance", []) if value]
        with db.open_db(self.db_path) as connection:
            dashboard = analytics.build_dashboard(connection)
            rebalance_items = self._select_rebalance_items(dashboard["allocation"]["product"], selected_labels)
            custom_rebalance = analytics.suggest_rebalance(
                rebalance_items,
                dashboard["summary"]["cash_total"],
                dashboard["summary"]["total_market_value"],
                analytics.parse_rebalance_targets(raw_targets),
            )
        return self._render_dashboard(
            start_response,
            message="Rebalance targets updated.",
            custom_rebalance=custom_rebalance,
            custom_targets=raw_targets,
            selected_rebalance_labels=selected_labels,
        )

    def _render_dashboard(
        self,
        start_response,
        status: str = "200 OK",
        message: Optional[str] = None,
        error: Optional[str] = None,
        custom_rebalance: Optional[Dict[str, object]] = None,
        custom_targets: Optional[str] = None,
        selected_rebalance_labels: Optional[list[str]] = None,
    ):
        with db.open_db(self.db_path) as connection:
            dashboard = analytics.build_dashboard(connection)
            page_market_sync = self._maybe_auto_refresh_market_data(connection, dashboard)
            if page_market_sync.get("triggered") and page_market_sync.get("status") == "success":
                dashboard = analytics.build_dashboard(connection)
            market_sync = self._build_market_sync_view(connection, dashboard, page_market_sync)
        rebalance = custom_rebalance or dashboard["rebalance"]
        selected_rebalance_labels = selected_rebalance_labels or []
        chart_payload = json.dumps(
            {
                "nav": dashboard["timeseries"]["nav"],
                "contribution": dashboard["timeseries"]["net_contribution"],
                "profit": dashboard["timeseries"]["profit"],
                "productProfitBreakdown": dashboard["timeseries"]["product_profit_breakdown"],
                "baseCurrency": dashboard["summary"]["base_currency"],
            },
            ensure_ascii=False,
        )
        rate_chart_payload = json.dumps(
            {
                "totalTwr": dashboard["timeseries"]["total_twr"],
                "effectiveTwr": dashboard["timeseries"]["effective_twr"],
                "peakCostRate": dashboard["timeseries"]["peak_cost_rate"],
                "benchmarks": {},
            },
            ensure_ascii=False,
        )
        template = self.templates.get_template("index.html")
        body = template.render(
            dashboard=dashboard,
            rebalance=rebalance,
            message=message,
            error=error,
            sample_pdf_path=str(DEFAULT_SAMPLE_PDF),
            sample_xml_path=str(DEFAULT_SAMPLE_XML),
            ibkr_service_url=ibkr_client.FLEX_SERVICE_URL,
            market_sync=market_sync,
            chart_payload=chart_payload,
            rate_chart_payload=rate_chart_payload,
            custom_targets=custom_targets or _default_targets_text(rebalance["targets"]),
            selected_rebalance_labels=selected_rebalance_labels,
        )
        return self._respond(start_response, status, body, "text/html; charset=utf-8")

    @staticmethod
    def _current_holding_instrument_ids(dashboard: Dict[str, object]) -> list[str]:
        products = dashboard.get("products") or []
        ranked = sorted(
            (
                item for item in products
                if item.get("status") == "open"
                and (item.get("quantity") or 0.0) > 0
                and item.get("instrument_id")
            ),
            key=lambda item: item.get("market_value_base", 0.0),
            reverse=True,
        )
        return [item["instrument_id"] for item in ranked]

    def _maybe_auto_refresh_market_data(self, connection, dashboard: Dict[str, object]) -> Dict[str, object]:
        instrument_ids = self._current_holding_instrument_ids(dashboard)
        if not instrument_ids:
            return {"triggered": False, "status": "skipped", "reason": "no_open_positions"}

        last_success = self._latest_market_sync(connection, success_only=True)
        if last_success and not _is_sync_stale(last_success.get("completed_at"), AUTO_MARKET_REFRESH_INTERVAL):
            return {"triggered": False, "status": "fresh", "reason": "within_refresh_window"}

        run_id = str(uuid.uuid4())
        started_at = _timestamp()
        db.insert_sync_run(
            connection,
            {
                "id": run_id,
                "source": "market_data_auto",
                "started_at": started_at,
                "completed_at": None,
                "status": "running",
                "detail_json": {
                    "mode": "targeted",
                    "instrument_ids": instrument_ids,
                    "instrument_count": len(instrument_ids),
                },
            },
        )
        try:
            result = market_data.refresh_market_data(connection, instrument_ids=instrument_ids, include_fx=False)
            completed_at = _timestamp()
            detail = {
                **result,
                "trigger": "page_load",
            }
            db.insert_sync_run(
                connection,
                {
                    "id": run_id,
                    "source": "market_data_auto",
                    "started_at": started_at,
                    "completed_at": completed_at,
                    "status": "success",
                    "detail_json": detail,
                },
            )
            return {
                "triggered": True,
                "status": "success",
                "completed_at": completed_at,
                "detail": detail,
            }
        except Exception as exc:
            completed_at = _timestamp()
            db.insert_sync_run(
                connection,
                {
                    "id": run_id,
                    "source": "market_data_auto",
                    "started_at": started_at,
                    "completed_at": completed_at,
                    "status": "failed",
                    "detail_json": {
                        "mode": "targeted",
                        "instrument_ids": instrument_ids,
                        "instrument_count": len(instrument_ids),
                        "trigger": "page_load",
                        "error": str(exc),
                    },
                },
            )
            return {
                "triggered": True,
                "status": "failed",
                "completed_at": completed_at,
                "error": str(exc),
            }

    def _build_market_sync_view(self, connection, dashboard: Dict[str, object], page_event: Dict[str, object]) -> Dict[str, object]:
        last_success = self._latest_market_sync(connection, success_only=True)
        last_event = self._latest_market_sync(connection, success_only=False)
        active_instrument_ids = self._current_holding_instrument_ids(dashboard)
        detail = (last_success or {}).get("detail_json") or {}
        page_status = "这次打开页面没有触发自动刷新。"
        if page_event.get("status") == "success":
            page_status = (
                f"这次打开页面已经自动定向刷新当前持仓，共 {detail.get('instrument_count', len(active_instrument_ids))} 个产品。"
            )
        elif page_event.get("status") == "failed":
            page_status = f"这次自动刷新失败：{page_event.get('error')}"
        elif page_event.get("status") == "fresh":
            page_status = f"最近 {int(AUTO_MARKET_REFRESH_INTERVAL.total_seconds() // 60)} 分钟内已经刷过，这次直接复用。"
        elif page_event.get("reason") == "no_open_positions":
            page_status = "当前没有持有中的产品，所以这次没有自动刷新行情。"

        return {
            "active_count": len(active_instrument_ids),
            "auto_window_minutes": int(AUTO_MARKET_REFRESH_INTERVAL.total_seconds() // 60),
            "last_success_at": _format_sync_time((last_success or {}).get("completed_at")),
            "last_status": (last_event or {}).get("status") or "never",
            "last_source": _sync_source_label((last_success or {}).get("source")),
            "last_mode": _sync_mode_label(detail.get("mode")),
            "last_price_rows": detail.get("prices", 0),
            "last_fx_rows": detail.get("fx_rates", 0),
            "last_scope_count": detail.get("instrument_count", len(active_instrument_ids)),
            "page_status": page_status,
            "page_triggered": bool(page_event.get("triggered")),
        }

    @staticmethod
    def _latest_market_sync(connection, success_only: bool) -> Optional[Dict[str, object]]:
        placeholders = ", ".join("?" for _ in MARKET_SYNC_SOURCES)
        query = (
            f"SELECT * FROM sync_runs WHERE source IN ({placeholders}) "
            + ("AND status = 'success' " if success_only else "")
            + "ORDER BY COALESCE(completed_at, started_at) DESC LIMIT 1"
        )
        row = db.fetch_one(connection, query, list(MARKET_SYNC_SOURCES))
        if not row:
            return None
        payload = dict(row)
        try:
            payload["detail_json"] = json.loads(payload.get("detail_json") or "{}")
        except json.JSONDecodeError:
            payload["detail_json"] = {}
        return payload

    @staticmethod
    def _parse_form(environ) -> Dict[str, str]:
        parsed = PortfolioApplication._parse_form_values(environ)
        return {key: values[0] for key, values in parsed.items()}

    @staticmethod
    def _parse_form_values(environ) -> Dict[str, list[str]]:
        size = int(environ.get("CONTENT_LENGTH") or 0)
        raw = environ["wsgi.input"].read(size).decode("utf-8")
        return parse_qs(raw)

    @staticmethod
    def _parse_multipart_form(environ):
        content_type = environ.get("CONTENT_TYPE", "")
        if not content_type.lower().startswith("multipart/form-data"):
            raise ValueError("Database restore requires a multipart file upload.")
        size = int(environ.get("CONTENT_LENGTH") or 0)
        if size <= 0:
            raise ValueError("The database upload is empty.")
        if size > MAX_BACKUP_UPLOAD_BYTES:
            raise ValueError("Database backup exceeds the 256 MB upload limit.")
        raw = environ["wsgi.input"].read(size)
        message = BytesParser(policy=email_policy).parsebytes(
            b"Content-Type: "
            + content_type.encode("ascii")
            + b"\r\nMIME-Version: 1.0\r\n\r\n"
            + raw
        )
        if not message.is_multipart():
            raise ValueError("Unable to parse the database upload.")

        fields: Dict[str, str] = {}
        files: Dict[str, tuple[str, bytes]] = {}
        for part in message.iter_parts():
            if part.get_content_disposition() != "form-data":
                continue
            name = part.get_param("name", header="content-disposition")
            if not name:
                continue
            payload = part.get_payload(decode=True) or b""
            filename = part.get_filename()
            if filename is not None:
                files[name] = (Path(filename).name, payload)
                continue
            charset = part.get_content_charset() or "utf-8"
            fields[name] = payload.decode(charset)
        return fields, files

    @staticmethod
    def _select_rebalance_items(items: list[Dict[str, object]], selected_labels: list[str]) -> list[Dict[str, object]]:
        if not selected_labels:
            return items
        selected_set = set(selected_labels)
        return [item for item in items if item["label"] in selected_set]

    @staticmethod
    def _respond(start_response, status: str, body: str, content_type: str):
        payload = body.encode("utf-8")
        return PortfolioApplication._respond_bytes(start_response, status, payload, content_type)

    @staticmethod
    def _respond_bytes(start_response, status: str, payload: bytes, content_type: str, extra_headers=None):
        start_response(
            status,
            [
                ("Content-Type", content_type),
                ("Content-Length", str(len(payload))),
                *(extra_headers or []),
            ],
        )
        return [payload]

def _default_targets_text(targets: Dict[str, float]) -> str:
    return "\n".join(f"{key}: {value:.2f}" for key, value in targets.items())


def _timestamp() -> str:
    return _iso_now()


def _iso_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_iso_datetime(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    normalized = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _is_sync_stale(completed_at: Optional[str], ttl: timedelta) -> bool:
    completed = _parse_iso_datetime(completed_at)
    if not completed:
        return True
    return datetime.now(timezone.utc) - completed > ttl


def _format_sync_time(value: Optional[str]) -> str:
    parsed = _parse_iso_datetime(value)
    if not parsed:
        return "还没有成功同步过"
    return parsed.astimezone().strftime("%Y-%m-%d %H:%M:%S")


def _sync_source_label(source: Optional[str]) -> str:
    if source == "market_data_auto":
        return "自动定向刷新"
    if source == "market_data_manual":
        return "手动全量刷新"
    return "尚未刷新"


def _sync_mode_label(mode: Optional[str]) -> str:
    if mode == "targeted":
        return "当前持仓定向刷新"
    if mode == "full":
        return "全量刷新"
    return "未知"
