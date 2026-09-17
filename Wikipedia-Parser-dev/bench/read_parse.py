#!/usr/bin/env python3
"""Benchmark: reader-only vs full-pipeline (empty writers) throughput, to locate the read/parse bottleneck.

    python bench/read_parse.py [dump.xml.bz2] [N]

- Does not connect to a database (writers are no-ops); measures only "read + parse + collect".
- N = max pages to process (default 6000; larger is closer to steady state, and slower).
- Reading the output: if reader-only is clearly faster than full across all workers, reading is not the bottleneck;
  once full stops getting faster past some workers, it has topped out (usually limited by the main-process GIL stage).
"""

import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "pipeline"))
import engine  # noqa: E402
from xml_source import XmlBatchSource  # noqa: E402

DUMP = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "simplewiki-latest-pages-articles.xml.bz2"
N = int(sys.argv[2]) if len(sys.argv) > 2 else int(os.environ.get("N", "6000"))


class Null:
    def __init__(self) -> None:
        self.n = 0

    def add_rows(self, rows) -> None:
        self.n += len(rows)

    def close(self) -> None:
        pass


def full(workers: int) -> tuple[int, float]:
    src = XmlBatchSource(DUMP, chunk_size=500, max_pages=N)
    t = time.monotonic()
    p, _ = engine._run_core(
        src, Null(), workers=workers, total=N, raw_writer=Null(),
        task_timeout=None, source_close=src.close,
    )
    return p, time.monotonic() - t


if __name__ == "__main__":
    if not DUMP.exists():
        sys.exit(f"dump does not exist: {DUMP}\nDownload *-pages-articles.xml.bz2 here first, or pass it as the first argument.")
    cores = os.cpu_count() or 8
    print(f"dump={DUMP.name}  N={N}  cores={cores}")

    src = XmlBatchSource(DUMP, chunk_size=500, max_pages=N)
    t = time.monotonic()
    rn = sum(len(b) for b in src)
    src.close()
    print(f"reader-only         : {rn / (time.monotonic() - t):5.0f} rec/s  <- single-core decompress+XML ceiling")

    for w in sorted({2, 4, 8, cores}):
        p, dt = full(w)
        print(f"full  workers={w:<3}   : {p / dt:5.0f} rec/s")
