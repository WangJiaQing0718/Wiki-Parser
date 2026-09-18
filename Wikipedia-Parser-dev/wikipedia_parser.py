#!/usr/bin/env python3
"""
Combined pipeline: single-pass read of an XML dump -> fan out to two outputs.

  XML dump ──single-pass stream──┬──▶ simplewiki_latest    (raw copy, always written)
                                 └──▶ multiprocess mwparserfromhell parse ──▶ simplewiki_processed
                                                                               wiki_component_* tables
                                                                               simplewiki_sections / _paragraph / _wtp_intermediate / _sentence

Compared with the two-step approach ("first XML->DB, then read DB->process"),
this flow reads the 350MB dump only once and decompresses only once. The raw write
and the component writes run **in parallel along two paths**.

- The raw table (simplewiki_latest) has a fixed structure, written by `xml_source.SQLServerWriter`.
- The parse chain (sections -> per-section components -> paragraphs -> sentences) writes
  `simplewiki_processed`, seven component tables (infobox/table/wikilinks/external_links/
  file/image/ref) and the three hierarchical tables
  (simplewiki_sections / simplewiki_paragraph / simplewiki_wtp_intermediate / simplewiki_sentence).
- The orchestration core `engine._run_core`: batch source (XML stream) + process-pool parse + dual write,
  including backpressure/timeout/first-error/cleanup hardening.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Callable

# Modules under pipeline/: the XML side (raw writer + streaming batch source) and
# the processing engine. Put it first on sys.path so a multiprocess-spawn worker
# can also re-import engine by module name (where process_batch lives).
sys.path.insert(0, str(Path(__file__).resolve().parent / "pipeline"))
import engine  # noqa: E402
from checkpoint import SQLServerCheckpointStore, source_id_for_path  # noqa: E402
from pipeline_config import load_pipeline_config, resolve_dump_file  # noqa: E402
from run_logging import start_run_log  # noqa: E402
from xml_source import (  # noqa: E402
    SQLServerBatchSource,
    SQLServerWriter,
    SkippingBatchSource,
    XmlBatchSource,
)


def progress_total_for_source(*, is_xml: bool, max_pages: int | None) -> int | None:
    """Return a trustworthy percentage denominator for a source type."""
    # XML's max_pages limits physical <page> elements. Namespace filtering and
    # logical-resume filtering happen later, so it is not the count processed
    # by Read/Parse/Write/Raw. Keep those streaming bars count-and-rate only.
    return None if is_xml else max_pages


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Single-pass XML dump read: writes the raw table simplewiki_latest, simplewiki_processed, seven wiki_component_* tables (infobox/table/wikilinks/external_links/file/image/ref), WTP intermediate output, and the hierarchical tables simplewiki_sections / simplewiki_paragraph / simplewiki_sentence."
    )
    p.add_argument(
        "dump_file", type=Path, nargs="?",
        help="Wikipedia dump path (*.xml.bz2 or *.xml). Omit with --source-table.",
    )
    p.add_argument(
        "--config", type=Path, required=True,
        help="Unified config JSON containing source, sqlserver, and wtp settings.",
    )
    p.add_argument(
        "--source-table", default=None,
        help="Read existing SQL Server rows instead of an XML dump (e.g. simplewiki_latest).",
    )
    p.add_argument(
        "--processed-table",
        default="simplewiki_processed",
        help="Processed table name (one row per article; text_process = wikitext with components replaced by their ids). Default simplewiki_processed.",
    )
    p.add_argument(
        "--table-prefix",
        default="wiki_component",
        help="Prefix for the component tables; each type writes to <prefix>_<type>. Default wiki_component. Raw table name comes from the config's table (default simplewiki_latest).",
    )
    p.add_argument(
        "--sections-table",
        default="simplewiki_sections",
        help="Sections table base name (one row per section; delete-then-insert per flush on revision_id). Default simplewiki_sections.",
    )
    p.add_argument(
        "--paragraphs-table",
        default="simplewiki_paragraph",
        help="Paragraphs table base name (one row per paragraph; delete-then-insert per flush on revision_id). Default simplewiki_paragraph.",
    )
    p.add_argument(
        "--wtp-intermediate-table",
        default="simplewiki_wtp_intermediate",
        help="WTP intermediate table (one row per paragraph: WTP input and expanded Wikitext; delete-then-insert per revision_id). Default simplewiki_wtp_intermediate.",
    )
    p.add_argument(
        "--sentences-table",
        default="simplewiki_sentence",
        help="Sentences table base name (one row per sentence; delete-then-insert per flush on revision_id). Default simplewiki_sentence.",
    )
    p.add_argument("--workers", type=int, default=None, help="Number of parse processes; defaults to CPU cores.")
    p.add_argument("--chunk-size", type=int, default=500, help="Pages per batch.")
    p.add_argument("--write-batch", type=int, default=500, help="Batch upsert size for both tables.")
    p.add_argument("--max-pages", type=int, default=None, help="Max number of pages to process; all if omitted.")
    p.add_argument(
        "--namespace", type=int, action="append", default=None,
        help="Only process these namespaces (repeatable). Default: 0 (articles). Use --all-namespaces for all.",
    )
    p.add_argument("--all-namespaces", action="store_true", help="Process all namespaces (overrides --namespace).")
    p.add_argument(
        "--task-timeout", type=float, default=300.0,
        help="Max wait in seconds per batch parse; prevents a wedged/very-slow worker from hanging the main flow; 0 disables. Default 300.",
    )
    p.add_argument(
        "--db-timeout", type=float, default=300.0,
        help="DB query timeout in seconds (reads/writes for both tables); prevents infinite blocking on network jitter / a stalled server; 0 disables. Default 300.",
    )
    p.add_argument("--db-login-timeout", type=float, default=60.0, help="DB connect/login timeout in seconds. Default 60.")
    p.add_argument(
        "--resume", action=argparse.BooleanOptionalAction, default=True,
        help="For XML input, skip terminal revisions recorded in the checkpoint table. Default: enabled.",
    )
    p.add_argument(
        "--retry-errors", action="store_true",
        help="With XML resume, process completed_with_errors revisions again.",
    )
    p.add_argument(
        "--checkpoint-table", default="simplewiki_parse_checkpoint",
        help="SQL Server table used for durable XML resume state. Default simplewiki_parse_checkpoint.",
    )
    return p


def _run(
    args: argparse.Namespace,
    parser: argparse.ArgumentParser,
    failure_logger: Callable[[str], None] | None = None,
) -> None:
    import os

    if not args.config.exists():
        parser.error(f"--config file does not exist: {args.config}")
    unified_config = load_pipeline_config(args.config)
    config = unified_config.sqlserver
    wtp_settings = unified_config.wtp
    configured_dump_file = unified_config.dump_file

    dump_file = resolve_dump_file(
        cli_dump_file=args.dump_file,
        source_table=args.source_table,
        configured_dump_file=configured_dump_file,
    )

    # Argument bounds validation: give a friendly error early.
    if bool(dump_file) == bool(args.source_table):
        parser.error("provide exactly one input: dump_file or --source-table")
    if dump_file is not None and not dump_file.exists():
        parser.error(f"dump file does not exist: {dump_file}")
    if args.workers is not None and args.workers < 1:
        parser.error("--workers must be >= 1 (leave empty for CPU cores by default).")
    if args.chunk_size < 1:
        parser.error("--chunk-size must be >= 1.")
    if args.write_batch < 1:
        parser.error("--write-batch must be >= 1.")
    if args.max_pages is not None and args.max_pages < 1:
        parser.error("--max-pages must be >= 1 (all pages if omitted).")
    if args.task_timeout < 0:
        parser.error("--task-timeout must be >= 0 (0 disables the timeout guard).")
    if args.db_timeout < 0:
        parser.error("--db-timeout must be >= 0 (0 disables the query timeout).")
    if args.db_login_timeout < 1:
        parser.error("--db-login-timeout must be >= 1.")

    schema = config.get("schema", "dbo")
    workers = args.workers or os.cpu_count() or 4
    # None = all namespaces; default keeps articles only (ns 0).
    namespaces = None if args.all_namespaces else set(args.namespace or [0])

    # XML imports retain the raw-table fan-out.  A SQL source is already raw
    # data, so it is deliberately not written back to simplewiki_latest.
    raw_writer = None
    if dump_file is not None:
        raw_writer = SQLServerWriter(
            config,
            batch_size=args.write_batch,
            db_timeout=args.db_timeout,
            login_timeout=args.db_login_timeout,
        )
    processed_writer = engine.ProcessedTextWriter(
        config, schema, args.processed_table, batch_size=args.write_batch,
        db_timeout=args.db_timeout, login_timeout=args.db_login_timeout,
    )
    component_writer = engine.ComponentWriter(
        config, schema, batch_size=args.write_batch, table_prefix=args.table_prefix,
        db_timeout=args.db_timeout, login_timeout=args.db_login_timeout,
    )
    sections_writer = engine.SectionsWriter(
        config, schema, args.sections_table, batch_size=args.write_batch,
        db_timeout=args.db_timeout, login_timeout=args.db_login_timeout,
    )
    paragraphs_writer = engine.ParagraphsWriter(
        config, schema, args.paragraphs_table, batch_size=args.write_batch,
        db_timeout=args.db_timeout, login_timeout=args.db_login_timeout,
    )
    wtp_intermediate_writer = engine.WtpIntermediateWriter(
        config, schema, args.wtp_intermediate_table, batch_size=args.write_batch,
        db_timeout=args.db_timeout, login_timeout=args.db_login_timeout,
    )
    sentences_writer = engine.SentencesWriter(
        config, schema, args.sentences_table, batch_size=args.write_batch,
        db_timeout=args.db_timeout, login_timeout=args.db_login_timeout,
    )
    sink = engine.FanoutWriter(
        processed_writer, component_writer,
        sections_writer, paragraphs_writer, wtp_intermediate_writer, sentences_writer,
    )

    checkpoint_store = None
    if dump_file is not None:
        source = XmlBatchSource(
            dump_file, args.chunk_size, max_pages=args.max_pages,
            namespaces=namespaces,
        )
        source_name = str(dump_file)
        if args.resume:
            source_id = source_id_for_path(dump_file)
            checkpoint_store = SQLServerCheckpointStore(
                config, schema, args.checkpoint_table, source_id,
                db_timeout=args.db_timeout, login_timeout=args.db_login_timeout,
            )
            terminal_ids = checkpoint_store.load_terminal_ids(
                retry_errors=args.retry_errors
            )
            print(
                f"Resume: checkpoint_table={args.checkpoint_table}, "
                f"source_id={source_id[:12]}, loaded_terminal={len(terminal_ids)}, "
                f"retry_errors={args.retry_errors}"
            )
            source = SkippingBatchSource(
                source,
                terminal_ids,
            )
    else:
        source = SQLServerBatchSource(
            config, schema, args.source_table, args.chunk_size,
            max_pages=args.max_pages, namespaces=namespaces,
            db_timeout=args.db_timeout, login_timeout=args.db_login_timeout,
        )
        source_name = f"{schema}.{args.source_table}"

    checkpoint = engine.CheckpointCoordinator(checkpoint_store) if checkpoint_store else None
    try:
        articles, parse_failed = engine._run_core(
            source,
            sink,
            workers=workers,
            total=progress_total_for_source(
                is_xml=dump_file is not None,
                max_pages=args.max_pages,
            ),
            raw_writer=raw_writer,
            task_timeout=args.task_timeout or None,
            max_count=None,  # limiting is done at the read side via --max-pages
            key_column=engine.KEY_COLUMN,  # revision_id
            source_close=source.close,
            wtp_settings=wtp_settings,
            checkpoint=checkpoint,
            failure_logger=failure_logger,
        )
    finally:
        if checkpoint_store is not None:
            checkpoint_store.close()

    raw_table = config.get("table", "simplewiki_latest")
    counts = component_writer.written
    comp_summary = ", ".join(f"{t}={counts[t]}" for t in engine.COMPONENT_TYPES)
    print(
        f"Done. articles={articles}, parse_failed={parse_failed}, workers={workers}, "
        f"source={source_name}, raw_table={raw_table}, processed_table={args.processed_table}"
    )
    print(
        f"  hierarchical (rows): sections={sections_writer.written}, "
        f"paragraphs={paragraphs_writer.written}, "
        f"wtp_intermediate={wtp_intermediate_writer.written}, "
        f"sentences={sentences_writer.written}"
    )
    print(f"  components (rows): {comp_summary}")
    if checkpoint_store is not None:
        print(
            f"  resume: checkpoint_table={args.checkpoint_table}, "
            f"source_id={checkpoint_store.source_id[:12]}, "
            f"skipped_terminal={source.skipped}"
        )
    if parse_failed:
        print(f"WARNING: {parse_failed} articles failed to parse (see [parse-fail] logs above).")


def main() -> None:
    parser = build_parser()
    session = start_run_log(Path(__file__).resolve().parent / "logs")
    try:
        print(f"Log: {session.path}")
        args = parser.parse_args()
        _run(args, parser, session.write_parse_failure)
    finally:
        session.close()


if __name__ == "__main__":
    main()
