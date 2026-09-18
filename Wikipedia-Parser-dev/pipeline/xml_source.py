#!/usr/bin/env python3
"""
XML side: stream a Wikipedia dump into "batches" and provide the raw-table writer.

  * build_latest_record -- take each page's latest revision, assemble one raw record.
  * XmlBatchSource      -- stream-read the dump (bz2), yield batches of records (for the engine to consume).
  * SQLServerWriter     -- batched upsert writer for the raw table simplewiki_latest (fixed structure).

The raw-table structure is fixed by this module (matching the dump fields); the
component tables are defined by engine.ComponentWriter.
"""

from __future__ import annotations

import bz2
from pathlib import Path
from typing import Any, Iterator

import mwxml
import pymssql


def build_latest_record(page: Any, latest_revision: Any) -> dict[str, Any]:
    """One page's latest revision -> raw record. Adds a `content` alias so the parse path can read it directly."""
    record = {
        "page_id": page.id,
        "page_title": page.title,
        "namespace": page.namespace,
        "is_redirect": page.redirect is not None,
        "revision_id": latest_revision.id,
        "comment": latest_revision.comment,
        "user_text": getattr(latest_revision.user, "text", None),
        "user_id": getattr(latest_revision.user, "id", None),
        "minor": latest_revision.minor,
        "model": latest_revision.model,
        "format": latest_revision.format,
        "text": latest_revision.text,
    }
    record["content"] = record["text"]  # the parse path (process_batch) reads content
    return record


class XmlBatchSource:
    """Stream the XML dump into "batches (lists of records)" for the engine's _run_core to consume.

    Each record serves both paths: the raw writer reads all keys of
    build_latest_record; parsing reads content. Iteration opens the stream;
    close() closes it (the engine calls it during cleanup).
    """

    def __init__(
        self,
        dump_path: Path,
        chunk_size: int,
        max_pages: int | None = None,
        namespaces: set[int] | None = None,
    ) -> None:
        self.dump_path = dump_path
        self.chunk_size = chunk_size
        self.max_pages = max_pages
        # None = all namespaces; otherwise keep only pages whose namespace is in the set
        # (e.g. {0} for articles only).
        self.namespaces = namespaces
        self._stream: Any = None

    def __iter__(self) -> Iterator[list[dict[str, Any]]]:
        if self.dump_path.suffix == ".bz2":
            self._stream = bz2.open(self.dump_path, mode="rb")
        else:
            self._stream = self.dump_path.open("rb")
        dump = mwxml.Dump.from_file(self._stream)
        batch: list[dict[str, Any]] = []
        page_count = 0
        for page in dump:
            page_count += 1
            if self.namespaces is not None and page.namespace not in self.namespaces:
                continue  # skip non-matching namespaces (e.g. templates/categories)
            latest_revision = None
            for revision in page:
                latest_revision = revision
            if latest_revision is not None:
                batch.append(build_latest_record(page, latest_revision))
                if len(batch) >= self.chunk_size:
                    yield batch
                    batch = []
            if self.max_pages is not None and page_count >= self.max_pages:
                break
        if batch:
            yield batch

    def close(self) -> None:
        if self._stream is not None:
            self._stream.close()


class SkippingBatchSource:
    """Filter terminal revisions while leaving an underlying stream sequential."""

    def __init__(self, source: Any, terminal_revision_ids: set[int]) -> None:
        self.source = source
        self.terminal_revision_ids = terminal_revision_ids
        self.skipped = 0

    def __iter__(self) -> Iterator[list[dict[str, Any]]]:
        for batch in self.source:
            remaining = [
                row for row in batch
                if int(row["revision_id"]) not in self.terminal_revision_ids
            ]
            self.skipped += len(batch) - len(remaining)
            if remaining:
                yield remaining

    def close(self) -> None:
        self.source.close()


class SQLServerBatchSource:
    """Read existing ``simplewiki_latest`` rows in batches without reimporting XML."""

    def __init__(
        self,
        config: dict[str, Any],
        schema: str,
        table: str,
        chunk_size: int,
        max_pages: int | None = None,
        namespaces: set[int] | None = None,
        db_timeout: float = 300.0,
        login_timeout: float = 60.0,
    ) -> None:
        self.config = config
        self.schema = schema
        self.table = table
        self.chunk_size = chunk_size
        self.max_pages = max_pages
        self.namespaces = namespaces
        self.db_timeout = db_timeout
        self.login_timeout = login_timeout
        self._conn: Any = None

    @staticmethod
    def _quote_ident(name: str) -> str:
        if not name:
            raise ValueError("Schema and table names must be non-empty.")
        return "[" + name.replace("]", "]]" ) + "]"

    @property
    def _qualified(self) -> str:
        return f"{self._quote_ident(self.schema)}.{self._quote_ident(self.table)}"

    def __iter__(self) -> Iterator[list[dict[str, Any]]]:
        self._conn = pymssql.connect(
            server=self.config["host"], port=int(self.config.get("port", 1433)),
            user=self.config["user"], password=self.config["password"],
            database=self.config["database"], charset="utf8", autocommit=False,
            timeout=int(self.db_timeout), login_timeout=int(self.login_timeout),
        )
        top = f"TOP ({int(self.max_pages)}) " if self.max_pages else ""
        where, params = "", []
        if self.namespaces is not None:
            placeholders = ", ".join(["%s"] * len(self.namespaces))
            where = f"WHERE [namespace] IN ({placeholders})"
            params = sorted(self.namespaces)
        sql = f"""
            SELECT {top}[revision_id], [page_id], [page_title], [namespace],
                   [content], [content_model] AS [model]
            FROM {self._qualified}
            {where}
            ORDER BY [page_id], [revision_id]
        """
        with self._conn.cursor(as_dict=True) as cursor:
            cursor.execute(sql, params)
            while rows := cursor.fetchmany(self.chunk_size):
                yield [dict(row) for row in rows]

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()


class SQLServerWriter:
    """Batched upsert writer for the raw table simplewiki_latest (fixed structure, matching the dump fields)."""

    # Raw-table column order; write_record must build rows in this order.
    _COLUMNS = (
        "revision_id", "page_id", "page_title", "namespace", "is_redirect",
        "comment", "user_text", "user_id", "minor",
        "content_model", "content_format", "content",
    )

    def __init__(
        self,
        config: dict[str, Any],
        batch_size: int = 500,
        db_timeout: float = 0.0,
        login_timeout: float = 60.0,
    ) -> None:
        self.schema = config.get("schema", "dbo")
        # `or` (not .get default) so an explicit null "table" in the JSON also falls back.
        self.table = config.get("table") or "simplewiki_latest"
        self.batch_size = batch_size
        self._buffer: list[tuple[Any, ...]] = []
        self._qualified_table = self._qualified_name(self.schema, self.table)
        self._object_name = f"{self.schema}.{self.table}"
        self._merge_sql = self._build_merge_sql()

        # db_timeout = query timeout (seconds), 0 = unlimited; the combined
        # pipeline passes a non-zero value to prevent DB writes from blocking
        # forever on network jitter or a stalled server. login_timeout =
        # connect/login timeout.
        self._conn = pymssql.connect(
            server=config["host"],
            port=int(config.get("port", 1433)),
            user=config["user"],
            password=config["password"],
            database=config["database"],
            charset="utf8",
            autocommit=False,
            timeout=int(db_timeout),
            login_timeout=int(login_timeout),
        )
        self._create_table_if_not_exists()

    @staticmethod
    def _quote_ident(name: str) -> str:
        if not name:
            raise ValueError("Schema and table names must be non-empty.")
        return "[" + name.replace("]", "]]") + "]"

    @classmethod
    def _qualified_name(cls, schema: str, table: str) -> str:
        return f"{cls._quote_ident(schema)}.{cls._quote_ident(table)}"

    def _create_table_if_not_exists(self) -> None:
        sql = f"""
        IF OBJECT_ID(N'{self._qualified_table}', N'U') IS NULL
        BEGIN
            CREATE TABLE {self._qualified_table} (
                [revision_id] BIGINT NOT NULL PRIMARY KEY,
                [page_id] BIGINT NOT NULL,
                [page_title] NVARCHAR(512) NOT NULL,
                [namespace] INT NOT NULL,
                [is_redirect] BIT NOT NULL,
                [comment] NVARCHAR(MAX) NULL,
                [user_text] NVARCHAR(255) NULL,
                [user_id] BIGINT NULL,
                [minor] BIT NULL,
                [content_model] NVARCHAR(64) NULL,
                [content_format] NVARCHAR(128) NULL,
                [content] NVARCHAR(MAX) NULL
            );
        END
        IF COL_LENGTH(N'{self._object_name}', N'revision_timestamp') IS NOT NULL
        BEGIN
            DECLARE @revision_timestamp_default SYSNAME;
            DECLARE @drop_revision_timestamp_default NVARCHAR(MAX);
            SELECT @revision_timestamp_default = dc.name
            FROM sys.default_constraints AS dc
            INNER JOIN sys.columns AS c
                ON c.object_id = dc.parent_object_id
               AND c.column_id = dc.parent_column_id
            WHERE dc.parent_object_id = OBJECT_ID(N'{self._object_name}')
              AND c.name = N'revision_timestamp';
            IF @revision_timestamp_default IS NOT NULL
            BEGIN
                SET @drop_revision_timestamp_default =
                    N'ALTER TABLE {self._qualified_table} DROP CONSTRAINT '
                    + QUOTENAME(@revision_timestamp_default);
                EXEC sys.sp_executesql @drop_revision_timestamp_default;
            END
            ALTER TABLE {self._qualified_table} DROP COLUMN [revision_timestamp];
        END
        """
        with self._conn.cursor() as cursor:
            cursor.execute(sql)
        self._conn.commit()

    def write_record(self, record: dict[str, Any]) -> None:
        row = (
            record["revision_id"],
            record["page_id"],
            record["page_title"],
            record["namespace"],
            int(bool(record["is_redirect"])),
            record["comment"],
            record["user_text"],
            record["user_id"],
            int(bool(record["minor"])) if record["minor"] is not None else None,
            record["model"],
            record["format"],
            record["text"],
        )
        self._buffer.append(row)
        if len(self._buffer) >= self.batch_size:
            self.flush()

    def add_rows(self, records: list[dict[str, Any]]) -> None:
        """Batched write (the raw-writer interface used by the combined pipeline)."""
        for record in records:
            self.write_record(record)

    def _build_merge_sql(self) -> str:
        cols = ", ".join(self._COLUMNS)
        set_clause = ",\n                ".join(f"{c} = source.{c}" for c in self._COLUMNS)
        source_cols = ", ".join(f"source.{c}" for c in self._COLUMNS)
        return f"""
        MERGE {self._qualified_table} AS target
        USING (VALUES {{values}}) AS source ({cols})
        ON target.revision_id = source.revision_id
        WHEN MATCHED THEN
            UPDATE SET
                {set_clause}
        WHEN NOT MATCHED THEN
            INSERT ({cols})
            VALUES ({source_cols});
        """

    def flush(self) -> None:
        if not self._buffer:
            return
        # Chunked multi-row MERGE: one round trip per chunk instead of one per
        # row; the chunk size keeps each statement below SQL Server's
        # 2100-parameter limit (13 columns/row -> 153 rows).
        rows_per_stmt = max(1, 2000 // len(self._COLUMNS))
        row = "(" + ", ".join(["%s"] * len(self._COLUMNS)) + ")"
        try:
            with self._conn.cursor() as cursor:
                for i in range(0, len(self._buffer), rows_per_stmt):
                    chunk = self._buffer[i:i + rows_per_stmt]
                    cursor.execute(
                        self._merge_sql.format(values=",\n            ".join([row] * len(chunk))),
                        [v for r in chunk for v in r],
                    )
            self._conn.commit()
            self._buffer.clear()
        except Exception:
            self._conn.rollback()
            raise

    def close(self) -> None:
        self.flush()
        self._conn.close()
