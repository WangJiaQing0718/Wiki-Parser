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

# Modules under pipeline/: the XML side (raw writer + streaming batch source) and
# the processing engine. Put it first on sys.path so a multiprocess-spawn worker
# can also re-import engine by module name (where process_batch lives).
sys.path.insert(0, str(Path(__file__).resolve().parent / "pipeline"))
import engine  # noqa: E402
from xml_source import SQLServerBatchSource, SQLServerWriter, XmlBatchSource  # noqa: E402
from wtp_integration import load_wtp_settings  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Single-pass XML dump read: writes the raw table simplewiki_latest, simplewiki_processed, seven wiki_component_* tables (infobox/table/wikilinks/external_links/file/image/ref), WTP intermediate output, and the hierarchical tables simplewiki_sections / simplewiki_paragraph / simplewiki_sentence."
    )
    p.add_argument(
        "dump_file", type=Path, nargs="?",
        help="Wikipedia dump path (*.xml.bz2 or *.xml). Omit with --source-table.",
    )
    p.add_argument("--db-config", type=Path, required=True, help="DB config JSON path.")
    p.add_argument(
        "--wtp-config", type=Path, required=True,
        help="WTP config JSON (normally points at WikiData/current.json).",
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
    return p


def main() -> None:
    import os

    parser = build_parser()
    args = parser.parse_args()

    # Argument bounds validation: give a friendly error early.
    if bool(args.dump_file) == bool(args.source_table):
        parser.error("provide exactly one input: dump_file or --source-table")
    if args.dump_file is not None and not args.dump_file.exists():
        parser.error(f"dump file does not exist: {args.dump_file}")
    if not args.db_config.exists():
        parser.error(f"--db-config file does not exist: {args.db_config}")
    if not args.wtp_config.exists():
        parser.error(f"--wtp-config file does not exist: {args.wtp_config}")
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

    config = engine.load_config(args.db_config)
    wtp_settings = load_wtp_settings(args.wtp_config)
    schema = config.get("schema", "dbo")
    workers = args.workers or os.cpu_count() or 4
    # None = all namespaces; default keeps articles only (ns 0).
    namespaces = None if args.all_namespaces else set(args.namespace or [0])

    # XML imports retain the raw-table fan-out.  A SQL source is already raw
    # data, so it is deliberately not written back to simplewiki_latest.
    raw_writer = None
    if args.dump_file is not None:
        raw_writer = SQLServerWriter(
            args.db_config,
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

    if args.dump_file is not None:
        source = XmlBatchSource(
            args.dump_file, args.chunk_size, max_pages=args.max_pages,
            namespaces=namespaces,
        )
        source_name = str(args.dump_file)
    else:
        source = SQLServerBatchSource(
            config, schema, args.source_table, args.chunk_size,
            max_pages=args.max_pages, namespaces=namespaces,
            db_timeout=args.db_timeout, login_timeout=args.db_login_timeout,
        )
        source_name = f"{schema}.{args.source_table}"

    articles, parse_failed = engine._run_core(
        source,
        sink,
        workers=workers,
        total=args.max_pages,  # streaming source: a total is known only when limited
        raw_writer=raw_writer,
        task_timeout=args.task_timeout or None,
        max_count=None,  # limiting is done at the read side via --max-pages
        key_column=engine.KEY_COLUMN,  # revision_id
        source_close=source.close,
        wtp_settings=wtp_settings,
    )

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
    if parse_failed:
        print(f"WARNING: {parse_failed} articles failed to parse (see [parse-fail] logs above).")


if __name__ == "__main__":
    main()
