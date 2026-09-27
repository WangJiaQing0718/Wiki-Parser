#!/usr/bin/env python3
"""基准测试：模拟数据库写入延迟，以观察何时写入成为瓶颈。

    python bench/write_latency.py [dump.xml.bz2] [N] [workers]

- 写入器每批休眠一点时间（休眠会释放 GIL，就像真实的 pymssql 数据库往返一样）；两个写入器
  （raw + processed）在自己的线程上并行运行，瓶颈是较慢的那一个。
- 单写入器上限 = 500000 / L(ms) 行/秒；当它下降到接近"read+parse ceiling"时，
  写入开始拖慢整个流水线。
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
N = int(sys.argv[2]) if len(sys.argv) > 2 else int(os.environ.get("N", "5000"))
WORKERS = int(sys.argv[3]) if len(sys.argv) > 3 else 4


class SleepWriter:
    def __init__(self, ms_per_batch: float) -> None:
        self.sec = ms_per_batch / 1000.0
        self.n = 0

    def add_rows(self, rows) -> None:
        time.sleep(self.sec)  # simulate one batch upsert's DB round trip
        self.n += len(rows)

    def close(self) -> None:
        pass


def run(latency_ms: float) -> float:
    src = XmlBatchSource(DUMP, chunk_size=500, max_pages=N)
    t = time.monotonic()
    p, _ = engine._run_core(
        src, SleepWriter(latency_ms), workers=WORKERS, total=N,
        raw_writer=SleepWriter(latency_ms), task_timeout=None, source_close=src.close,
    )
    return p / (time.monotonic() - t)


if __name__ == "__main__":
    if not DUMP.exists():
        sys.exit(f"dump does not exist: {DUMP}\nDownload *-pages-articles.xml.bz2 here first, or pass it as the first argument.")
    print(f"dump={DUMP.name}  N={N}  workers={WORKERS}")
    print(f"{'write lat/batch':>16} {'single-writer cap':>18} {'measured':>10}")
    for L in (0, 50, 200, 500, 1000, 1500):
        cap = "∞" if L == 0 else f"{500000 / L:.0f}/s"
        print(f"{L:>7}ms {cap:>14} {run(L):>8.0f}/s")
