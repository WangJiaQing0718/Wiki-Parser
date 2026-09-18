# XML Logical Resume Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Resume interrupted XML imports by scanning from the beginning and skipping terminal revisions only after every output sink has committed.

**Architecture:** A small SQL Server checkpoint store identifies a dump by path metadata and loads terminal revision IDs at startup.  A filtering source removes those rows before the existing raw/parse pipeline.  A thread-safe barrier receives committed acknowledgements from both writer branches and records the final status only when both are durable.

**Tech Stack:** Python 3.12, `pymssql`, SQL Server, `mwxml`, `unittest`.

**Spec:** `docs/superpowers/specs/2026-09-17-xml-resume-design.md`

## Global Constraints

- Apply resume behavior only when the positional XML input is used.
- Treat `completed` and `completed_with_errors` as terminal by default.
- Never create a checkpoint before the raw and every processed sink batch have committed.
- Preserve current idempotent output behavior and existing `--source-table` semantics.

---

### Task 1: Checkpoint data boundary

**Files:**
- Create: `pipeline/checkpoint.py`
- Create: `tests/test_checkpoint.py`

**Interfaces:**
- Produces `source_id_for_path(path: Path) -> str`.
- Produces `CheckpointStore.load_terminal_ids(retry_errors: bool) -> set[int]` and `CheckpointStore.mark_terminal(records: list[dict[str, Any]]) -> None`.

- [ ] **Step 1: Write failing tests**

```python
def test_checkpoint_status_uses_parse_error():
    store = MemoryCheckpointStore()
    store.mark_terminal([
        {"revision_id": 1, "page_id": 10, "parse_error": None},
        {"revision_id": 2, "page_id": 20, "parse_error": "Lua error"},
    ])
    assert store.load_terminal_ids(retry_errors=False) == {1, 2}
    assert store.load_terminal_ids(retry_errors=True) == {1}
```

- [ ] **Step 2: Run the failing test**

Run: `C:\Users\WangJiaQing\.conda\envs\py312\python.exe -m unittest tests.test_checkpoint -v`

Expected: FAIL because the checkpoint module does not exist.

- [ ] **Step 3: Implement the minimal checkpoint store**

Create a SQL Server table keyed by `(source_id, revision_id)`, query terminal
IDs, and use one transaction per `mark_terminal` batch.  Store `completed`
for null `parse_error` and `completed_with_errors` otherwise.

- [ ] **Step 4: Run the checkpoint tests**

Run: `C:\Users\WangJiaQing\.conda\envs\py312\python.exe -m unittest tests.test_checkpoint -v`

Expected: PASS.

### Task 2: Filtering source and commit barrier

**Files:**
- Modify: `pipeline/xml_source.py`
- Modify: `pipeline/engine.py`
- Modify: `tests/test_checkpoint.py`

**Interfaces:**
- Produces `SkippingBatchSource(source, terminal_ids)` with `skipped` counter and `close()` delegation.
- Produces `CheckpointCoordinator(store)` with `raw_committed(batch_id)` and `processed_committed(batch_id, bundles)`.

- [ ] **Step 1: Write failing tests**

```python
def test_filter_drops_only_terminal_revisions():
    source = [[{"revision_id": 1}], [{"revision_id": 2}, {"revision_id": 3}]]
    wrapped = SkippingBatchSource(source, {1, 3})
    assert list(wrapped) == [[{"revision_id": 2}]]
    assert wrapped.skipped == 2

def test_checkpoint_waits_for_both_commits():
    store = MemoryCheckpointStore()
    gate = CheckpointCoordinator(store)
    gate.processed_committed(7, [{"revision_id": 7, "page_id": 1, "parse_error": None}])
    assert store.rows == []
    gate.raw_committed(7)
    assert [row["revision_id"] for row in store.rows] == [7]
```

- [ ] **Step 2: Run the failing tests**

Run: `C:\Users\WangJiaQing\.conda\envs\py312\python.exe -m unittest tests.test_checkpoint -v`

Expected: FAIL because the filtering source and coordinator do not exist.

- [ ] **Step 3: Implement filtering and durable acknowledgements**

Add `flush()` to `FanoutWriter`.  Attach a monotonically increasing batch ID
to raw and future queue entries.  On each branch, call its writer `flush()`
before acknowledging the coordinator.  The coordinator writes checkpoints
only after both acknowledgement types for the same batch are present.

- [ ] **Step 4: Run the focused tests**

Run: `C:\Users\WangJiaQing\.conda\envs\py312\python.exe -m unittest tests.test_checkpoint -v`

Expected: PASS.

### Task 3: CLI wiring and regression verification

**Files:**
- Modify: `wikipedia_parser.py`
- Modify: `tests/test_checkpoint.py`
- Modify: `docs/architecture.md`

**Interfaces:**
- Adds `--no-resume`, `--retry-errors`, and `--checkpoint-table`.
- Passes an optional checkpoint coordinator only for XML source runs.

- [ ] **Step 1: Write a failing CLI/default test**

```python
def test_xml_resume_is_enabled_by_default():
    args = build_parser().parse_args(["dump.xml.bz2", "--config", "config.json"])
    assert args.resume is True
    assert args.retry_errors is False
```

- [ ] **Step 2: Run the failing test**

Run: `C:\Users\WangJiaQing\.conda\envs\py312\python.exe -m unittest tests.test_checkpoint -v`

Expected: FAIL because the parser does not expose resume options.

- [ ] **Step 3: Wire the store, filtering source, and reporting**

Create the store only for XML input when resume is enabled, wrap the source
with loaded terminal IDs, close the store during cleanup, and print skipped
record count.  Document the status guarantee in `docs/architecture.md`.

- [ ] **Step 4: Run unit and smoke verification**

Run: `C:\Users\WangJiaQing\.conda\envs\py312\python.exe -m unittest discover -s tests -v`

Expected: PASS.

Run a two-pass XML smoke import with `--max-pages 20` against a disposable
checkpoint table.  Expected: the second pass reports terminal rows skipped;
with `--retry-errors`, only rows with parse errors are submitted again.
