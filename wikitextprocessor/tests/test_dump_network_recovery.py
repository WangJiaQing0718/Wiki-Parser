import importlib.util
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests

from wikitextprocessor import Wtp
from wikitextprocessor.dumpparser import process_dump
from wikitextprocessor.interwiki import get_interwiki_data


class DumpNetworkRecoveryTests(unittest.TestCase):
    def test_pages_are_committed_before_interwiki_initialization(self):
        """A failed interwiki request must not roll back extracted pages."""
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "dump.db"
            wtp = Wtp(db_path=db_path)
            try:
                def extract_one_page(wtp, _path, _namespace_ids):
                    wtp.add_page("Saved page", 0, "body")

                with (
                    patch(
                        "wikitextprocessor.dumpparser.parse_dump_xml",
                        side_effect=extract_one_page,
                    ),
                    patch(
                        "wikitextprocessor.dumpparser.init_interwiki_map",
                        side_effect=requests.ConnectionError("connection reset"),
                    ),
                ):
                    with self.assertRaises(requests.ConnectionError):
                        process_dump(wtp, "unused.xml.bz2", {0})

                connection = sqlite3.connect(db_path)
                try:
                    page_count = connection.execute(
                        "SELECT COUNT(*) FROM pages WHERE title = ?", ("Saved page",)
                    ).fetchone()[0]
                finally:
                    connection.close()
                self.assertEqual(page_count, 1)
            finally:
                wtp.close_db_conn()

    def test_interwiki_request_retries_after_transient_connection_error(self):
        """A temporary API disconnect should not abort dump processing."""
        wtp = Mock(lang_code="en", project="wikipedia")
        response = Mock(ok=True)
        response.json.return_value = {
            "query": {"interwikimap": [{"prefix": "s", "url": "https://x/$1"}]}
        }

        with (
            patch(
                "requests.get",
                side_effect=[requests.ConnectionError("reset"), response],
            ) as request_get,
            patch("time.sleep"),
        ):
            try:
                result = get_interwiki_data(wtp)
            except requests.ConnectionError:
                result = []

        self.assertEqual(result, [{"prefix": "s", "url": "https://x/$1"}])
        self.assertEqual(request_get.call_count, 2)
        self.assertIn("timeout", request_get.call_args.kwargs)

    def test_interwiki_request_uses_empty_map_after_all_retries_fail(self):
        """A permanently unavailable API must not abort local dump processing."""
        wtp = Mock(lang_code="en", project="wikipedia")

        with (
            patch("requests.get", side_effect=requests.ConnectionError("reset")) as request_get,
            patch("time.sleep"),
        ):
            try:
                result = get_interwiki_data(wtp)
            except requests.ConnectionError:
                result = ["request failed"]

        self.assertEqual(result, [])
        self.assertEqual(request_get.call_count, 3)


class XmlToDbCleanupTests(unittest.TestCase):
    def test_network_error_closes_the_wtp_database_connection(self):
        """The runner must close the Wtp database without masking its error."""
        script_path = Path(__file__).resolve().parents[2] / "xml-to-db.py"
        spec = importlib.util.spec_from_file_location("xml_to_db", script_path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            wiki_data_dir = temp_path / "WikiData"
            wiki_data_dir.mkdir()
            (wiki_data_dir / "sample.xml.bz2").touch()
            wtp = Mock(spec=["close_db_conn"])

            with (
                patch.object(module, "__file__", str(temp_path / "xml-to-db.py")),
                patch.object(module, "Wtp", return_value=wtp),
                patch.object(
                    module,
                    "process_dump",
                    side_effect=requests.ConnectionError("connection reset"),
                ),
                patch.object(sys, "argv", ["xml-to-db.py", "sample.xml.bz2"]),
            ):
                try:
                    module.main()
                except Exception as error:
                    raised_error = error
                else:
                    raised_error = None

            self.assertIsInstance(raised_error, requests.ConnectionError)
            wtp.close_db_conn.assert_called_once_with()
