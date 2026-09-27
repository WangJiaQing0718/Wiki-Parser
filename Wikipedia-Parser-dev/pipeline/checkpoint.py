"""用于逻辑 XML 导入恢复的持久化终端状态存储。"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Mapping

import pymssql


COMPLETED = "completed"
COMPLETED_WITH_ERRORS = "completed_with_errors"


def source_id_for_path(path: Path) -> str:
    """为转储的这个精确路径/大小/mtime 版本返回一个稳定的 ID。"""
    stat = path.stat()
    identity = "\0".join(
        ("xml-logical-resume-v1", str(path.resolve()), str(stat.st_size), str(stat.st_mtime_ns))
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def terminal_records_for(
    source_id: str, bundles: list[dict[str, Any]]
) -> list[tuple[str, int, int, str]]:
    """将已提交的解析包转换为 checkpoint 行。"""
    return [
        (
            source_id,
            int(bundle["revision_id"]),
            int(bundle["page_id"]),
            COMPLETED_WITH_ERRORS if bundle.get("parse_error") else COMPLETED,
        )
        for bundle in bundles
    ]


def quote_ident(name: str) -> str:
    if not name:
        raise ValueError("标识符必须非空。")
    return "[" + name.replace("]", "]]" ) + "]"


class SQLServerCheckpointStore:
    """由专用 SQL 连接的 checkpoint 表支持的一个源 ID 的 checkpoint 表。"""

    def __init__(
        self,
        config: Mapping[str, Any],
        schema: str,
        table: str,
        source_id: str,
        *,
        db_timeout: float = 300.0,
        login_timeout: float = 60.0,
    ) -> None:
        self.source_id = source_id
        self.schema = schema
        self.table = table
        self._qualified = f"{quote_ident(schema)}.{quote_ident(table)}"
        self._object_name = self._qualified.replace("'", "''")
        self._conn = pymssql.connect(
            server=config["host"], port=int(config.get("port", 1433)),
            user=config["user"], password=config["password"],
            database=config["database"], charset="utf8", autocommit=False,
            timeout=int(db_timeout), login_timeout=int(login_timeout),
        )
        self._create_table_if_needed()

    def _create_table_if_needed(self) -> None:
        sql = f"""
        IF OBJECT_ID(N'{self._object_name}', N'U') IS NULL
        BEGIN
            CREATE TABLE {self._qualified} (
                [source_id] CHAR(64) NOT NULL,
                [revision_id] BIGINT NOT NULL,
                [page_id] BIGINT NOT NULL,
                [status] NVARCHAR(32) NOT NULL,
                [completed_at] DATETIME2(7) NOT NULL,
                PRIMARY KEY ([source_id], [revision_id]),
                CHECK ([status] IN (N'completed', N'completed_with_errors'))
            );
        END
        """
        with self._conn.cursor() as cursor:
            cursor.execute(sql)
        self._conn.commit()

    def load_terminal_ids(self, *, retry_errors: bool) -> set[int]:
        statuses = [COMPLETED]
        if not retry_errors:
            statuses.append(COMPLETED_WITH_ERRORS)
        placeholders = ", ".join(["%s"] * len(statuses))
        sql = (
            f"SELECT [revision_id] FROM {self._qualified} "
            f"WHERE [source_id] = %s AND [status] IN ({placeholders})"
        )
        with self._conn.cursor() as cursor:
            cursor.execute(sql, [self.source_id, *statuses])
            return {int(row[0]) for row in cursor.fetchall()}

    def mark_terminal(self, bundles: list[dict[str, Any]]) -> None:
        rows = terminal_records_for(self.source_id, bundles)
        if not rows:
            return
        value = "(%s, %s, %s, %s)"
        values = ",\n                ".join([value] * len(rows))
        sql = f"""
        MERGE {self._qualified} AS target
        USING (VALUES {values}) AS source ([source_id], [revision_id], [page_id], [status])
        ON target.[source_id] = source.[source_id]
           AND target.[revision_id] = source.[revision_id]
        WHEN MATCHED THEN UPDATE SET
            [page_id] = source.[page_id],
            [status] = source.[status],
            [completed_at] = SYSUTCDATETIME()
        WHEN NOT MATCHED THEN INSERT
            ([source_id], [revision_id], [page_id], [status], [completed_at])
        VALUES
            (source.[source_id], source.[revision_id], source.[page_id], source.[status], SYSUTCDATETIME());
        """
        try:
            with self._conn.cursor() as cursor:
                cursor.execute(sql, [value for row in rows for value in row])
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    def close(self) -> None:
        self._conn.close()
