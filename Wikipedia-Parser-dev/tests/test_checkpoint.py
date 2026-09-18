"""Regression tests for logical XML resume checkpoints."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch


PIPELINE_DIR = Path(__file__).resolve().parents[1] / "pipeline"
PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PIPELINE_DIR))
sys.path.insert(0, str(PROJECT_DIR))

from checkpoint import terminal_records_for  # noqa: E402
from engine import BatchResult, CheckpointCoordinator, _run_core  # noqa: E402
from xml_source import SkippingBatchSource  # noqa: E402
from wikipedia_parser import build_parser, progress_total_for_source  # noqa: E402


class CheckpointRecordTest(unittest.TestCase):
    def test_parse_error_is_persisted_as_completed_with_errors(self) -> None:
        """A status writer must not turn a parse-error article into a clean completion."""
        records = terminal_records_for(
            "source-a",
            [
                {"revision_id": 101, "page_id": 11, "parse_error": None},
                {"revision_id": 102, "page_id": 12, "parse_error": "Lua error"},
            ],
        )

        self.assertEqual(
            [
                ("source-a", 101, 11, "completed"),
                ("source-a", 102, 12, "completed_with_errors"),
            ],
            records,
        )

    def test_filter_drops_only_terminal_revisions(self) -> None:
        """A resumed scan must not submit known terminal revisions again."""
        source = _ListSource(
            [
                [{"revision_id": 1}],
                [{"revision_id": 2}, {"revision_id": 3}],
            ]
        )
        wrapped = SkippingBatchSource(source, {1, 3})

        self.assertEqual([[{"revision_id": 2}]], list(wrapped))
        self.assertEqual(2, wrapped.skipped)
        wrapped.close()
        self.assertTrue(source.closed)

    def test_checkpoint_waits_for_raw_and_processed_commits(self) -> None:
        """A parse result alone is insufficient: raw data must be durable too."""
        store = _RecordingStore()
        gate = CheckpointCoordinator(store)
        bundles = [{"revision_id": 7, "page_id": 70, "parse_error": None}]

        gate.processed_committed(4, bundles)
        self.assertEqual([], store.committed)
        gate.raw_committed(4)

        self.assertEqual([bundles], store.committed)

    def test_xml_resume_is_enabled_by_default(self) -> None:
        """Normal XML invocations should resume without requiring a new flag."""
        args = build_parser().parse_args(
            ["dump.xml.bz2", "--config", "config.json"]
        )

        self.assertTrue(args.resume)
        self.assertFalse(args.retry_errors)

    def test_xml_progress_has_no_false_max_pages_total(self) -> None:
        """A physical XML scan limit cannot be a parse/write percentage denominator."""
        self.assertIsNone(progress_total_for_source(is_xml=True, max_pages=20_000))
        self.assertEqual(
            20_000,
            progress_total_for_source(is_xml=False, max_pages=20_000),
        )

    def test_pipeline_marks_checkpoint_after_both_writer_flushes(self) -> None:
        """Removing either branch acknowledgement must leave this batch uncheckpointed."""
        raw_writer = _RecordingWriter()
        processed_writer = _RecordingWriter()
        store = _RecordingStore()
        rows = [[{"revision_id": 8, "page_id": 80, "page_title": "Eight", "namespace": 0}]]

        with patch("engine.ProcessPoolExecutor", _ImmediateExecutor):
            _run_core(
                rows,
                processed_writer,
                workers=1,
                raw_writer=raw_writer,
                task_timeout=1.0,
                checkpoint=CheckpointCoordinator(store),
            )

        self.assertGreaterEqual(raw_writer.flushes, 1)
        self.assertGreaterEqual(processed_writer.flushes, 1)
        self.assertEqual(1, len(store.committed))
        self.assertEqual(8, store.committed[0][0]["revision_id"])

    def test_pipeline_sends_only_parse_failure_lines_to_failure_logger(self) -> None:
        """The run log callback must receive the same detailed failure line as the console."""
        writer = _RecordingWriter()
        logged: list[str] = []
        rows = [[{"revision_id": 9, "page_id": 90, "page_title": "Nine", "namespace": 0}]]

        with patch("engine.ProcessPoolExecutor", _FailingExecutor):
            _run_core(
                rows,
                writer,
                workers=1,
                task_timeout=1.0,
                failure_logger=logged.append,
            )

        self.assertEqual(
            ["[parse-fail] revision_id=9: paragraph_errors=1; wtp_errors=1"],
            logged,
        )

    def test_pipeline_logs_failures_beyond_console_output_limit(self) -> None:
        """The file log must retain every failure even after console detail is capped."""
        writer = _RecordingWriter()
        logged: list[str] = []
        rows = [[{"revision_id": number, "page_id": number, "page_title": str(number), "namespace": 0}
                 for number in range(1, 22)]]

        with patch("engine.ProcessPoolExecutor", _ManyFailingExecutor):
            _run_core(
                rows,
                writer,
                workers=1,
                task_timeout=1.0,
                failure_logger=logged.append,
            )

        self.assertEqual(21, len(logged))
        self.assertEqual(
            "[parse-fail] revision_id=21: paragraph_errors=1; wtp_errors=1",
            logged[-1],
        )


class _ListSource:
    def __init__(self, batches: list[list[dict[str, int]]]) -> None:
        self.batches = batches
        self.closed = False

    def __iter__(self):
        yield from self.batches

    def close(self) -> None:
        self.closed = True


class _RecordingStore:
    def __init__(self) -> None:
        self.committed: list[list[dict[str, object]]] = []

    def mark_terminal(self, bundles: list[dict[str, object]]) -> None:
        self.committed.append(bundles)


class _RecordingWriter:
    def __init__(self) -> None:
        self.rows: list[dict[str, object]] = []
        self.flushes = 0

    def add_rows(self, rows: list[dict[str, object]]) -> None:
        self.rows.extend(rows)

    def flush(self) -> None:
        self.flushes += 1

    def close(self) -> None:
        pass


class _ImmediateFuture:
    def __init__(self, result: BatchResult) -> None:
        self._result = result

    def result(self, timeout: float) -> BatchResult:
        return self._result


class _ImmediateExecutor:
    def __init__(self, *args: object, **kwargs: object) -> None:
        pass

    def submit(self, _fn: object, rows: list[dict[str, object]], _timeout: float) -> _ImmediateFuture:
        records = [
            {
                "revision_id": row["revision_id"],
                "page_id": row["page_id"],
                "parse_error": None,
            }
            for row in rows
        ]
        return _ImmediateFuture(BatchResult(records=records, failures=[]))

    def shutdown(self, **_kwargs: object) -> None:
        pass


class _FailingExecutor(_ImmediateExecutor):
    def submit(self, _fn: object, rows: list[dict[str, object]], _timeout: float) -> _ImmediateFuture:
        records = [
            {
                "revision_id": row["revision_id"],
                "page_id": row["page_id"],
                "parse_error": "paragraph_errors=1; wtp_errors=1",
            }
            for row in rows
        ]
        return _ImmediateFuture(
            BatchResult(
                records=records,
                failures=[(9, "paragraph_errors=1; wtp_errors=1")],
            )
        )


class _ManyFailingExecutor(_ImmediateExecutor):
    def submit(self, _fn: object, rows: list[dict[str, object]], _timeout: float) -> _ImmediateFuture:
        error = "paragraph_errors=1; wtp_errors=1"
        return _ImmediateFuture(
            BatchResult(
                records=[
                    {"revision_id": row["revision_id"], "page_id": row["page_id"], "parse_error": error}
                    for row in rows
                ],
                failures=[(row["revision_id"], error) for row in rows],
            )
        )


if __name__ == "__main__":
    unittest.main()
