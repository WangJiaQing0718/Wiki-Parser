# Wikipedia Parser

Reads a Wikipedia XML dump in a single pass, **simultaneously** storing the raw data into a database and using multiple processes to, for each ns=0 article, run the chain **sections -> per-section components -> paragraphs -> WTP intermediate -> sentences** and produce: a `simplewiki_processed` row (the wikitext with each extracted component replaced in place by its component id), per-type component tables holding the extracted infoboxes / tables / wikilinks / external links / files / images / refs, and the detail tables `simplewiki_sections` / `simplewiki_paragraph` / `simplewiki_wtp_intermediate` / `simplewiki_sentence`. Wiki lists remain in the paragraph flow for sentence-stage list handling.

```
                         ┌──▶ simplewiki_latest      (raw copy, fixed schema)
simplewiki-*.xml.bz2 ──single pass──┤
                         │        ┌──▶ simplewiki_processed   (text_process: markup kept, components -> ids)
                         └──parse─┤
                                  ├──▶ wiki_component_{infobox,table,wikilinks,external_links,file,image,ref}
                                   └──▶ simplewiki_sections / simplewiki_paragraph / simplewiki_wtp_intermediate / simplewiki_sentence
```

The dump is read only once and decompressed only once; the raw write and the parse writes run in parallel.
See [`docs/architecture.md`](docs/architecture.md) for design and fault-tolerance details.

## Installation

```bash
# Install the local, patched ProjectB; do not replace it with PyPI.
python3 -m pip install --no-build-isolation -e ../wikitextprocessor
python3 -m pip install -r requirements.txt
```

## Database Configuration

```bash
cp config.example.json config.json     # fill in SQL Server / WTP / dump settings
```

`config.json` has three sections: `source.dump_file`, `sqlserver`, and `wtp`.
`sqlserver` contains `host`, `port` (default 1433), `user`, `password`, `database`,
`schema` (default `dbo`), and optionally `table` (raw table name, default
`simplewiki_latest`). `wtp.db_path` selects the versioned WTP SQLite database.

## Usage

```bash
python3 wikipedia_parser.py \
  --config config.json \
  --workers 8
```

To process rows already present in SQL Server (without importing/recompiling a dump):

```bash
python3 wikipedia_parser.py --source-table simplewiki_latest \
  --config config.json --max-pages 1000
```

The configured dump is used when no positional dump file is provided. A
positional dump file overrides `source.dump_file`; `--source-table` uses SQL
input instead. The WTP database is opened read-only with Wikidata HTTP fallback
disabled. Each process owns an independent WTP/Lua context.

At runtime, four speed bars refresh in sync -- `Read` (pages read) / `Parse` (parsing) / `Write` (processed table) / `Raw` (raw table):

```
Read : 12000 [00:14, 820 page/s]
Parse: 11900 [00:14, 815 row/s, failed=0]
Write: 11800 [00:14, 810 row/s]
Raw  : 12000 [00:14, 820 row/s]
```

Try processing 100 pages first:

```bash
python wikipedia_parser.py --config config.json --workers 16 --max-pages 10000
```

Parameters:

| Parameter | Default | Description |
| --- | --- | --- |
| `dump_file` | config value | Optional dump path override (`*.xml.bz2` or `*.xml`) |
| `--config` | required | Unified JSON containing `source`, `sqlserver`, and `wtp` |
| `--source-table` | off | Existing SQL Server raw source, e.g. `simplewiki_latest` |
| `--processed-table` | `simplewiki_processed` | Processed table (one row per article; `text_process`) |
| `--table-prefix` | `wiki_component` | Prefix for the component tables; each type writes to `<prefix>_<type>` |
| `--sections-table` | `simplewiki_sections` | Sections table (one row per section; delete-then-insert per flush on `revision_id`) |
| `--paragraphs-table` | `simplewiki_paragraph` | Paragraphs table (one row per paragraph; delete-then-insert per flush on `revision_id`) |
| `--wtp-intermediate-table` | `simplewiki_wtp_intermediate` | WTP input and expanded Wikitext per paragraph; delete-then-insert per flush on `revision_id` |
| `--sentences-table` | `simplewiki_sentence` | Sentences table (one row per sentence; delete-then-insert per flush on `revision_id`) |
| `--workers` | CPU cores | Number of parsing processes |
| `--chunk-size` | 500 | Pages per batch |
| `--write-batch` | 500 | Batch upsert size for both tables |
| `--max-pages` | all | Maximum number of pages to process |
| `--namespace` | 0 | Only process these namespaces (repeatable); default is 0 (articles) |
| `--all-namespaces` | off | Process all namespaces (overrides `--namespace`) |
| `--task-timeout` | 300 | Per-batch parse timeout in seconds, to prevent a worker from hanging; 0 disables |
| `--db-timeout` | 300 | DB query timeout in seconds; 0 disables |
| `--db-login-timeout` | 60 | DB connection/login timeout in seconds |

The raw table name is taken from the config's `table` (default `simplewiki_latest`).

## Paragraph WTP Expansion

For every section, MWP extracts the existing component rows and replaces the
recognized components with the existing `♣  ♣  ♣  <component-id>♣  ♣  ♣` placeholders.  No
component-name allow/deny list is applied.  The resulting paragraph is parsed by
MWP for structure analysis, expanded by WTP (templates, parser functions,
`#invoke`, Lua/Scribunto), and converted to visible text.  `simplewiki_paragraph`
stores `page_title`, `raw_wikitext`, final `text`, and `parse_error` in addition
to its existing revision/page/section/paragraph keys. `simplewiki_wtp_intermediate`
stores the MWP-replaced `wtp_input` and WTP's `expanded_wikitext` before visible-text
conversion, using the same paragraph natural key. Per-paragraph WTP/Lua errors are
recorded in both tables without stopping the rest of the page or batch.

## Processed Table + Component Tables

For each ns=0 article, `engine.extract_and_templatize()` produces:

- one **`simplewiki_processed`** row whose `text_process` is the original wikitext with **each
  extracted component replaced in place by `♣  ♣  ♣  <component-id>♣  ♣  ♣`** (club delimiters mark the
  placeholder; other markup kept). **Wikilinks are the exception**: they are extracted to their table
  but **left in place as raw `[[...]]`** in `text_process` (not replaced);
- rows in the **seven component tables**, each holding one component's extracted content, keyed by
  the same id.

**Extraction is by priority with strict containment.** Blocks (infobox / table) subsume
everything inside them; the inline components (wikilinks, external_links, `file` for
`[[File:...]]`, `image` for `[[Image:...]]`, `ref` for `<ref>...</ref>`) are only extracted at the
top level. So a `[[link]]`, `[[File:...]]`, or `<ref>` inside an infobox/table stays as raw
wikitext inside that component (not its own row); a `<ref>` or `[[File:...]]` likewise subsumes
anything nested in it. List blocks are not components and are also protected from nested component
extraction, so their raw hierarchy reaches the sentence-stage list handler.

`text_process` is thus the article with blocks/external-links replaced in place by
`♣  ♣  ♣  id♣  ♣  ♣`
placeholders (the original line structure is kept -- no extra blank lines are added around
placeholders), wikilinks and lists left raw.  Lists therefore reach WTP and then the sentence
extractor's list protection/rendering stage:

```
text_process:  "'''Bold''' [[Berlin]] says {{cite}}, see ♣  ♣  ♣  external_links-12-0002♣  ♣  ♣"
wiki_component_wikilinks:  wikilinks-12-0003 -> "Berlin"   (extracted, but [[Berlin]] stays in text)
wiki_component_external_links: external_links-12-0002 -> "https://..."  (replaced by its club-delimited placeholder)
```

**`simplewiki_processed`** (`--processed-table`, one row per article, MERGE upsert on `revision_id`):
`revision_id` (PK), `page_id`, `page_title`, `namespace`, `text_process`, `parse_error`.

**Component tables** `<prefix>_<type>` (default prefix `wiki_component`), all the same shape:

```sql
CREATE TABLE wiki_component_infobox (
    [id]           BIGINT IDENTITY(1,1) PRIMARY KEY,
    [page_id]      BIGINT NOT NULL,          -- owning page
    [infobox_id]   NVARCHAR(128) NOT NULL,   -- infobox-<page_id>-<seq>, e.g. infobox-12-0001
    [infobox_text] NVARCHAR(MAX) NULL,       -- extracted content
    CONSTRAINT [UQ_wiki_component_infobox_id] UNIQUE ([infobox_id])
);
CREATE INDEX [IX_wiki_component_infobox_page] ON wiki_component_infobox ([page_id]);
```

| Table | `<type>_text` content | rows per article |
| --- | --- | --- |
| `wiki_component_infobox` | raw wikitext of each `{{Infobox ...}}` | one per infobox |
| `wiki_component_table` | raw wikitext of each table (`{| ... |}` / `<table>`) | one per table |
| `wiki_component_wikilinks` | internal wikilink target title (top-level only) | one per prose `[[...]]` |
| `wiki_component_external_links` | external link URL (top-level only) | one per prose external link |
| `wiki_component_file` | raw `[[File:...]]` wikitext | one per top-level file link |
| `wiki_component_image` | raw `[[Image:...]]` wikitext | one per top-level image link |
| `wiki_component_ref` | raw `<ref>...</ref>` wikitext | one per top-level ref |

`<type>_id` is `<type>-<page_id>-<seq>` where `<seq>` is the **1-based occurrence order on the page**
(0001 = first infobox on that page, etc.), so the id in `text_process` matches the component row and
the source page is recoverable (`WHERE page_id = 12`).

> **All write paths are idempotent (safe to re-run).** The raw and processed tables are `MERGE`
> upserts on `revision_id`; the three hierarchical tables and the seven component tables are
> **delete-then-insert per flush** (one transaction: delete all rows of the keys re-inserted below
> -- by `revision_id` for the hierarchical tables, by `page_id` for the component tables), so
> re-running the same dump replaces rows in place with no duplicates, and rows from a changed
> parse chain are cleaned up automatically. Uniqueness is also enforced in the database:
> `revision_id` PRIMARY KEY on the raw/processed tables, `UNIQUE(revision_id, ...)` on the
> hierarchical tables, and a `UNIQUE` constraint on `<type>_id` plus a `page_id` index for each
> component table (so the flush DELETE can seek an index and a duplicate id fails loudly).
> Non-wikitext pages (Lua/JSON/CSS/JS) are kept verbatim with no components.
> Parse failures are counted (`parse_failed=N`) and recorded in `simplewiki_processed.parse_error`.

## Hierarchical Tables (sections / paragraphs / sentences)

`engine.build_bundle()` runs the same chain as API-Parser's extract stage (independent copies of the
`lib/` extractor modules, kept functionally aligned):

1. **Sections** (`section_extractor.extract_sections`): split by `== Title ==` (exactly two equals;
   `===` and deeper do NOT split). Content before the first `==` is `section_no=1, toc="Summary"`;
   an article with no heading is one whole Summary section.
2. **Per section**: component extraction + `remove_format_blocks()` (removes `<templatestyles>` /
   `<div>` formatting blocks). Sections whose `toc` is in the skip set (`references` /
   `other websites` / `related pages` / `more reading` / `category` / `external links` /
   `footnotes`) are kept verbatim: no component extraction, no format-block removal, no sentence
   segmentation. The article's `text_process` is the per-section processed texts joined with
   blank lines; each section that came from a `== Title ==` heading is prefixed with that
   original heading line, so section boundaries stay visible in `text_process` (the Summary
   section has no heading).
3. **Paragraphs** (`paragraph_extractor.extract_paragraphs`): blank-line split within each section;
   subsection headings (`===` and deeper) are removed from the text and update the colon-joined
   `toc` path (e.g. `Overview:History`); `paragraph_no` restarts at 1 per section.
4. **Sentences** (`sentence_extractor.extract_sentences`): `sentencex` segmentation; club-delimited ref
   placeholders removed first; list blocks are folded into a protected structure before
   segmentation so internal punctuation cannot split them; wikilinks are rendered as
   `[[target↓target]]` (`[[A|B]]` → `[[B↓A]]`); `sentence_no` is the 1-based order **within the
   section**.

Table shapes (all `revision_id`-keyed; **delete-then-insert per flush**: all rows of a revision are deleted and re-inserted in one transaction):

| Table | One row per | Natural key | Notable fields |
| --- | --- | --- | --- |
| `simplewiki_sections` | section | `(revision_id, section_no)` | `page_title`, `toc`, `raw_text`, `text` |
| `simplewiki_paragraph` | paragraph | `(revision_id, section_no, paragraph_no)` | `page_title`, `toc`, `raw_wikitext`, `text`, `parse_error` |
| `simplewiki_wtp_intermediate` | paragraph WTP boundary | `(revision_id, section_no, paragraph_no)` | `page_title`, `toc`, `wtp_input`, `expanded_wikitext`, `parse_error` |
| `simplewiki_sentence` | sentence | `(revision_id, section_no, paragraph_no, sentence_no)` | `page_title`, `toc`, `raw_text`, `text` |

Each hierarchical table has an `id` BIGINT IDENTITY primary key; no timestamp columns are retained.

```sql
-- A page's section -> paragraph -> sentence tree
SELECT s.toc, p.paragraph_no, p.text AS paragraph, t.sentence_no, t.text AS sentence
FROM simplewiki_sections AS s
JOIN simplewiki_paragraph AS p
  ON p.revision_id = s.revision_id AND p.section_no = s.section_no
JOIN simplewiki_sentence AS t
  ON t.revision_id = p.revision_id AND t.section_no = p.section_no
 AND t.paragraph_no = p.paragraph_no
WHERE s.page_id = @page_id
ORDER BY s.section_no, p.paragraph_no, t.sentence_no;
```

Example queries:

```sql
-- Reconstruct an article's skeleton + its infoboxes
SELECT p.text_process, i.infobox_id, i.infobox_text
FROM simplewiki_processed AS p
LEFT JOIN wiki_component_infobox AS i ON i.page_id = p.page_id
WHERE p.page_title = N'Foo';

-- Most-linked-to pages across the corpus
SELECT wikilinks_text AS target, COUNT(*) AS refs
FROM wiki_component_wikilinks
GROUP BY wikilinks_text
ORDER BY refs DESC;
```

## Code Structure

```
wikipedia_parser.py        entry point: CLI + orchestration wiring
pipeline/
├── xml_source.py          XML side: streaming batch source + raw table writer (fixed schema)
├── section_extractor.py   section split by == Title == (independent copy of API-Parser's lib/)
├── paragraph_extractor.py blank-line paragraph split + subsection toc path
├── sentence_extractor.py  sentencex sentence segmentation + wikilink/list handling
└── engine.py              processing engine: build_bundle chain + writers
                           (ProcessedTextWriter + ComponentWriter + Sections/Paragraphs/SentencesWriter)
docs/
├── architecture.md        architecture, fault tolerance, and data quality design notes
└── performance.md         performance and bottleneck measurements (why --workers 4 is the sweet spot)
```

- The core of `engine.py`, `_run_core(batches, processed_writer, raw_writer=...)`, is decoupled
  from the "batch source": this project drives it with an XML stream and attaches a raw writer.
- `engine.build_bundle()` runs the chain sections -> per-section components -> paragraphs ->
  sentences; `engine.FanoutWriter` fans each article bundle to `ProcessedTextWriter` (the processed
  table), `ComponentWriter` (the 7 component tables) and `SectionsWriter` / `ParagraphsWriter` /
  `SentencesWriter` (the 3 hierarchical tables).
- The raw table schema is fixed, corresponding to the dump fields, and is written by `xml_source.SQLServerWriter`.
