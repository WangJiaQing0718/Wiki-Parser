# Performance and Bottleneck Analysis

Benchmarks run against a real dump. **Key takeaways first**, methodology and reproduction scripts after.

## Conclusions at a Glance

- **Single-threaded reader is not the bottleneck**: single-core decompression + XML parsing can hit **~1400 rec/s**, yet the whole pipeline
  never exceeds ~660 rec/s -- the Reader has more than 2x headroom.
- **The real ceiling is two single-threaded, GIL-bound stages in the main process**: the **Reader** (decompression + XML parsing)
  and the **Collector** (deserializing each batch of parsed results back into the main process). The two contend for the same GIL, together yielding about
  **500-660 rec/s**, which 4 workers already saturate -- adding more cores barely helps.
- **At normal DB write latency, writing is not the bottleneck**: a single 500-row `MERGE` typically takes 10-100ms
  (= 2500-50000 rec/s), well above the ceiling. Only when a single batch's latency slows to **~800ms-1s or more**
  (slow network / large blob / heavy indexing / lock contention) does writing drag the pipeline down via backpressure.
- **Practical recommendation: `--workers 4`** is the sweet spot (setting it to 12 is actually slightly slower and just wastes cores). simplewiki's
  ~250k pages at ~500-600 rec/s ≈ **7-8 minutes** to finish.

> The numbers are strongly tied to the machine / dump / parsing logic (below is a 12-core Mac + 333MB simplewiki);
> **what transfers is the shape**: the Reader has headroom, the bottleneck is the main-process GIL stages, and writing stays hidden when the DB is healthy.

## Test Methodology

Each stage is **isolated** and timed separately, to avoid them masking one another:

1. **reader-only**: iterate only over `XmlBatchSource` (decompression + XML), no parsing, no writing → gives the single-core read ceiling.
2. **parse-only**: pre-read a batch of records into memory, repeatedly submit `process_batch` to the process pool, measuring parse throughput at different worker
   counts (main thread collects results).
3. **full (empty writer)**: real `XmlBatchSource` + process pool + **no-op writer** → isolates out the DB,
   showing only "read + parse + collect".
4. **write-latency sweep**: replace the writer with a per-batch `time.sleep(L)` (`sleep` releases the GIL, mimicking a real pymssql
   DB round-trip), sweeping different L to see when writing becomes the bottleneck.

## Environment

- CPU 12 cores; dump `simplewiki-latest-pages-articles.xml.bz2` (333MB); `chunk_size=500`.
- The writer is a no-op in tests 1-3 (to isolate the DB); test 4 uses simulated latency.

## Result 1: Read vs Parse (Which Is Faster)

| Stage | Throughput |
| --- | --- |
| reader-only (single-core decompress + XML) | **~1400 rec/s** |
| parse-only w=1 / 2 / 4 / 8 | 225 / 448 / 719 / **837** rec/s |

The Reader (1400) is faster than the process pool at full speed (~837) → **reading is not the bottleneck**.

## Result 2: Worker Scaling of the Full Pipeline (Empty Writer)

| workers | 2 | 4 | 6 | 8 | 12 |
| --- | --- | --- | --- | --- | --- |
| rec/s | 414 | **658** | 569 | 586 | 582 |

w=4 tops out (~660); beyond that, adding cores doesn't help and even regresses. full is actually **lower** than parse-only at the same worker count
(e.g. w=8: full 586 < parse-only 837) -- because the concurrent Reader (XML parsing, holds the GIL) and Collector
(result deserialization, holds the GIL) **contend for the same GIL**. `1/1400 + 1/837 ≈ 524 rec/s`, which matches the measurement,
confirming the bottleneck is these two serial main-process stages, not the process pool's compute.

## Result 3: Write-Latency Sweep (workers=4, two writers in parallel)

| Write latency/batch (500 rows) | Single-writer cap | Measured throughput |
| ---: | ---: | ---: |
| 0ms | ∞ | **494/s** (GIL ceiling) |
| 50ms | 10000/s | 482/s |
| 200ms | 2500/s | 451/s |
| 500ms | 1000/s | 421/s |
| 1000ms | 500/s | 364/s |
| 1500ms | 333/s | 275/s (writing is now the main bottleneck) |

**Crossover ~1s/batch**: when the single-writer cap drops to ~500/s (≈ the GIL ceiling), the two begin to drag on each other, and any slower it is
entirely determined by writing. Normal DB latency (10-100ms/batch) is well above that, so writing stays hidden.

This also **validates the two-queue fan-out design**: the raw and component write paths run **in parallel**, and the bottleneck is the **slower** of the two
(min); if merged into a single queue / single consumer, the two latencies would become **serial** at 2×L, halving the crossover point outright.

## To Break Past ~600 rec/s

The direction is to **reduce main-process GIL work**, not to add workers:

- Have the Collector deserialize less: workers return only the necessary fields (they currently return large component payloads, and both IPC and deserialization are expensive).
- **Sharded multiprocessing**: split the dump into several parts by page range and launch multiple independent `wikipedia_parser` processes, each with its own GIL
  (requires a multistream dump or a byte-offset index).
- Parallel decompression on the read side (`lbzip2 -dc | ...`) helps **this workload little** -- decompression is not the bottleneck, and it only speeds up the already-fast reader-only.

## Reproducing on Another Machine

The benchmark scripts ship with the repo: [`bench/read_parse.py`](../bench/read_parse.py),
[`bench/write_latency.py`](../bench/write_latency.py). They **do not connect to a database** (the writer is a no-op /
simulated latency), so you don't need to be able to reach SQL Server -- only that `pymssql` can import (the engine imports it at the top).

1. **Get the code + install dependencies**

   ```bash
   git clone <repo> && cd Wikipedia-Parser
   python3 -m venv .venv && . .venv/bin/activate
   pip install -r requirements.txt        # mwxml, mwparserfromhell, pymssql, tqdm
   ```

   > `pymssql` now has prebuilt wheels on most platforms and usually installs directly; the benchmark itself never actually connects to a database.

2. **Download a dump** (not included in the repo, ignored by `.gitignore`; any language/size works):

   ```bash
   curl -LO https://dumps.wikimedia.org/simplewiki/latest/simplewiki-latest-pages-articles.xml.bz2
   ```

3. **Run the benchmarks** (from the repo root; the scripts locate `pipeline/` relative to their own location, and `2>/dev/null` hides the tqdm speed bar so you only see the summary):

   ```bash
   # Read vs parse vs full pipeline (empty writer): shows reader-only and each workers
   python bench/read_parse.py 2>/dev/null
   # Specify dump / page count:  python bench/read_parse.py path/to/dump.xml.bz2 8000

   # Simulate DB write latency: see when writing becomes the bottleneck
   python bench/write_latency.py 2>/dev/null
   # Specify dump / page count / workers:  python bench/write_latency.py dump.xml.bz2 5000 4
   ```

4. **How to read the results** (the criteria are machine-independent):
   - `reader-only` is much faster than `full` at every workers → **reading is not the bottleneck**;
   - `full` stops getting faster past some workers → it has topped out (usually the main-process GIL stage), and that point is the **sweet-spot workers**;
   - in the write-latency table, when the "single-writer cap" drops to near the empty-writer `full` throughput, writing starts to slow things down → that is
     the **write-latency tolerance limit** for your machine + this dump.

> The numbers vary with machine / dump / `extract_and_templatize` parsing logic; the **shape** (reader has headroom, bottleneck is the GIL stage,
> writing stays hidden when the DB is healthy) is usually consistent. To stress-test against a real DB, just swap the no-op / SleepWriter in the scripts for
> `xml_source.SQLServerWriter` and `engine.ComponentWriter`.
