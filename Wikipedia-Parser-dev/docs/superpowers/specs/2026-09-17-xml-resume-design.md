# XML Logical Resume Design

## Goal

Allow an interrupted XML import to safely resume without repeating completed WTP
parsing.  The XML dump is still read sequentially from byte zero; terminal
revisions are filtered before they reach the raw or parse queues.

## Scope and constraints

- Applies to XML input only.  `--source-table` retains its current behavior.
- Default terminal states are `completed` and `completed_with_errors`.
- `completed_with_errors` is terminal by default because the user requested
  that parse-error articles count as completed.  `--retry-errors` makes only
  that state eligible for another attempt.
- Output tables are already idempotent by `revision_id`; a crash may repeat a
  fully or partly written batch, but may never skip an unwritten result.
- No random seek/index is introduced for the `.bz2` stream.  A measured
  full scan of this dump takes 118.416 seconds on the current machine.

## State model

`simplewiki_parse_checkpoint` is a SQL Server table, overrideable with
`--checkpoint-table`.  It stores one row per `(source_id, revision_id)`:

| Field | Meaning |
| --- | --- |
| `source_id` | SHA-256 of resolved XML path, file size, and modification time |
| `revision_id`, `page_id` | Stable source keys |
| `status` | `completed` or `completed_with_errors` |
| `completed_at` | UTC time at which all relevant sink writes had committed |

At startup, the state store loads terminal revision IDs for the current
`source_id` into memory.  The XML source is then scanned normally and a small
filter drops those records before raw writing and WTP submission.

## Commit ordering

For each batch:

1. The raw writer commits the original source records.
2. The processed fan-out commits processed text, components, sections,
   paragraphs, WTP intermediate rows, and sentences.
3. Only after both acknowledgements does the checkpoint store commit terminal
   rows, deriving `completed_with_errors` from non-null `parse_error`.

The raw and processed paths remain concurrent.  Each writer flushes the batch
before acknowledging it.  If a process dies at any earlier point, the
checkpoint is absent and the next run upserts the batch again.  If the
checkpoint exists, all outputs for that revision have already committed.

## CLI behavior

- XML imports resume by default.
- `--no-resume` disables filtering and checkpoint recording for a deliberate
  full reprocess.
- `--retry-errors` processes `completed_with_errors` rows again while still
  skipping `completed` rows.
- Startup and final output report the checkpoint table, source ID prefix, and
  count skipped by the filter.

## Verification

Unit tests cover terminal filtering, error status selection, and the two-path
commit barrier.  An XML smoke run with `--max-pages` is run twice: the second
run must report the same scanned source scope but zero or only retryable parse
submissions, and must not invoke WTP for terminal rows.
