"""Build or reuse the WTP SQLite database for a named XML dump in this directory."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Sequence


DATA_DIR = Path(__file__).resolve().parent
PIPELINE_DIR = DATA_DIR.parent / "Wikipedia-Parser-dev" / "pipeline"
sys.path.insert(0, str(PIPELINE_DIR))

from wtp_database import ensure_wtp_database  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build or reuse the WTP database for an XML dump in WikiData."
    )
    parser.add_argument(
        "dump_file",
        type=Path,
        help="XML dump filename or path relative to WikiData (*.xml.bz2).",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    dump_path = args.dump_file
    if not dump_path.is_absolute():
        dump_path = DATA_DIR / dump_path
    dump_path = dump_path.resolve()
    if not dump_path.is_file():
        parser.error(f"XML dump does not exist: {dump_path}")
    try:
        db_path = ensure_wtp_database(
            dump_path,
            {"lang_code": "en", "project": "wikipedia"},
        )
    except (OSError, ValueError, RuntimeError) as exc:
        parser.error(str(exc))
    print(f"WTP database: {db_path}")


if __name__ == "__main__":
    main()
