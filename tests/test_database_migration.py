import io
from pathlib import Path
import tempfile
import unittest

from portfolio_app import db
from portfolio_app.server import PortfolioApplication


class DatabaseMigrationTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

    def _database_with_account(self, filename: str, account_id: str) -> Path:
        path = self.root / filename
        db.init_db(path)
        with db.open_db(path) as connection:
            db.upsert_account(
                connection,
                {
                    "id": account_id,
                    "broker": "test",
                    "account_code": account_id,
                    "display_name": account_id,
                    "base_currency": "CNY",
                    "metadata_json": {},
                },
            )
        return path

    @staticmethod
    def _start_response_capture():
        captured = {}

        def start_response(status, headers):
            captured["status"] = status
            captured["headers"] = dict(headers)

        return captured, start_response

    @staticmethod
    def _multipart_body(database_bytes: bytes, confirm: bool = True):
        boundary = "portfolio-test-boundary"
        chunks = []
        if confirm:
            chunks.append(
                f"--{boundary}\r\n"
                'Content-Disposition: form-data; name="confirm_restore"\r\n\r\n'
                "1\r\n"
            .encode("utf-8"))
        chunks.append(
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="database_file"; filename="portfolio.db"\r\n'
            "Content-Type: application/vnd.sqlite3\r\n\r\n"
        .encode("utf-8") + database_bytes + b"\r\n")
        chunks.append(f"--{boundary}--\r\n".encode("utf-8"))
        body = b"".join(chunks)
        return boundary, body

    def test_export_route_returns_self_contained_sqlite_backup(self):
        database_path = self._database_with_account("current.db", "exported-account")
        app = PortfolioApplication(database_path)
        captured, start_response = self._start_response_capture()

        body = b"".join(app._export_database(start_response))

        self.assertEqual(captured["status"], "200 OK")
        self.assertEqual(captured["headers"]["Content-Type"], "application/vnd.sqlite3")
        self.assertIn("attachment;", captured["headers"]["Content-Disposition"])
        self.assertTrue(body.startswith(b"SQLite format 3\x00"))
        exported_path = self.root / "exported.db"
        exported_path.write_bytes(body)
        self.assertEqual(db.inspect_backup(exported_path)["accounts"], 1)

    def test_restore_route_replaces_database_and_keeps_rollback(self):
        target_path = self._database_with_account("target.db", "old-account")
        source_path = self._database_with_account("source.db", "new-account")
        app = PortfolioApplication(target_path)
        boundary, body = self._multipart_body(source_path.read_bytes())
        captured, start_response = self._start_response_capture()
        environ = {
            "REQUEST_METHOD": "POST",
            "PATH_INFO": "/actions/import-database",
            "CONTENT_TYPE": f"multipart/form-data; boundary={boundary}",
            "CONTENT_LENGTH": str(len(body)),
            "wsgi.input": io.BytesIO(body),
        }

        response = b"".join(app(environ, start_response)).decode("utf-8")

        self.assertEqual(captured["status"], "200 OK")
        self.assertIn("Restored 0 transactions", response)
        with db.open_db(target_path) as connection:
            account_ids = [row[0] for row in connection.execute("SELECT id FROM accounts")]
        self.assertEqual(account_ids, ["new-account"])
        rollback_files = list((target_path.parent / "backups" / "database").glob("*.db"))
        self.assertEqual(len(rollback_files), 1)
        with db.open_db(rollback_files[0]) as connection:
            rollback_ids = [row[0] for row in connection.execute("SELECT id FROM accounts")]
        self.assertEqual(rollback_ids, ["old-account"])

    def test_restore_requires_explicit_confirmation(self):
        target_path = self._database_with_account("target.db", "old-account")
        source_path = self._database_with_account("source.db", "new-account")
        app = PortfolioApplication(target_path)
        boundary, body = self._multipart_body(source_path.read_bytes(), confirm=False)
        captured, start_response = self._start_response_capture()
        environ = {
            "REQUEST_METHOD": "POST",
            "PATH_INFO": "/actions/import-database",
            "CONTENT_TYPE": f"multipart/form-data; boundary={boundary}",
            "CONTENT_LENGTH": str(len(body)),
            "wsgi.input": io.BytesIO(body),
        }

        b"".join(app(environ, start_response))

        self.assertEqual(captured["status"], "400 Bad Request")
        with db.open_db(target_path) as connection:
            account_ids = [row[0] for row in connection.execute("SELECT id FROM accounts")]
        self.assertEqual(account_ids, ["old-account"])

    def test_invalid_backup_is_rejected(self):
        invalid_path = self.root / "invalid.db"
        invalid_path.write_text("not a database", encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "not a SQLite"):
            db.inspect_backup(invalid_path)


if __name__ == "__main__":
    unittest.main()
