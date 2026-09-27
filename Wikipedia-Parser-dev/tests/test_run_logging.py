"""回归测试覆盖每次运行的控制台日志捕获。"""

from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path


PIPELINE_DIR = Path(__file__).resolve().parents[1] / "pipeline"
sys.path.insert(0, str(PIPELINE_DIR))

from run_logging import start_run_log  # noqa: E402


class RunLoggingTest(unittest.TestCase):
    def test_same_second_uses_a_distinct_log_file(self) -> None:
        """Rapid repeated starts must not fail because timestamp and PID match."""
        with tempfile.TemporaryDirectory() as tmp:
            log_dir = Path(tmp) / "logs"
            first = start_run_log(
                log_dir,
                now=datetime(2026, 9, 18, 14, 30, 15),
                pid=4321,
            )
            first.close()
            second = start_run_log(
                log_dir,
                now=datetime(2026, 9, 18, 14, 30, 15),
                pid=4321,
            )
            try:
                self.assertNotEqual(first.path, second.path)
            finally:
                second.close()

    def test_run_log_mirrors_stdout_and_stderr_immediately(self) -> None:
        """Only parse-failure records, not ordinary console output, belong in a log."""
        original_stdout, original_stderr = sys.stdout, sys.stderr
        with tempfile.TemporaryDirectory() as tmp:
            session = start_run_log(
                Path(tmp) / "logs",
                now=datetime(2026, 9, 18, 14, 30, 15),
                pid=4321,
            )
            try:
                print("Resume: loaded_terminal=7")
                session.write_parse_failure(
                    "[parse-fail] revision_id=7: paragraph_errors=1; wtp_errors=1"
                )
                print("Done. articles=7")
                self.assertEqual(
                    "[parse-fail] revision_id=7: paragraph_errors=1; wtp_errors=1\n",
                    session.path.read_text(encoding="utf-8"),
                )
                self.assertEqual("parser-20260918-143015-4321.log", session.path.name)
            finally:
                session.close()

        self.assertIs(sys.stdout, original_stdout)
        self.assertIs(sys.stderr, original_stderr)


if __name__ == "__main__":
    unittest.main()
