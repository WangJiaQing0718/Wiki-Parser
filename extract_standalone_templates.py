"""Copy standalone wikitext templates from WTP intermediate output to SQL Server."""

from __future__ import annotations

import pyodbc


SOURCE_DB_CONFIG = {
    "host": "localhost",
    "port": 1433,
    "user": "sa",
    "password": "Wang342688",
    "database": "Dev0921",
    "schema": "dbo",
}
TARGET_DB_CONFIG = {
    "host": "localhost",
    "port": 1433,
    "user": "sa",
    "password": "Wang342688",
    "database": "template",
    "schema": "dbo",
}

SOURCE_TABLE = "wiki_wtp_intermediate_[20260801]"
TARGET_TABLE = "template"
BATCH_SIZE = 1_000


def quote_identifier(identifier: str) -> str:
    """Quote one SQL Server identifier, including literal closing brackets."""
    if not identifier:
        raise ValueError("SQL identifier must not be empty")
    return "[" + identifier.replace("]", "]]") + "]"


def qualified_table(config: dict[str, object], table: str) -> str:
    return f"{quote_identifier(str(config['schema']))}.{quote_identifier(table)}"


def connect(config: dict[str, object]) -> pyodbc.Connection:
    connection_string = (
        "DRIVER={ODBC Driver 17 for SQL Server};"
        f"SERVER={config['host']},{config['port']};"
        f"DATABASE={config['database']};"
        f"UID={config['user']};"
        f"PWD={config['password']};"
        "TrustServerCertificate=yes;"
    )
    return pyodbc.connect(connection_string, timeout=30)


def standalone_template(wtp_input: object) -> tuple[str, str] | None:
    """Return a standalone template's name and text, if a paragraph is one template."""
    if not isinstance(wtp_input, str):
        return None
    candidate = wtp_input.strip()
    if not candidate:
        return None

    import mwparserfromhell
    from mwparserfromhell.nodes import Template

    nodes = mwparserfromhell.parse(candidate).nodes
    if len(nodes) == 1 and isinstance(nodes[0], Template):
        return str(nodes[0].name).strip(), str(nodes[0])
    return None


def progress_line(completed: int, total: int, candidates: int) -> str:
    """Render one in-place progress line for source-row processing."""
    width = 30
    ratio = min(completed / total, 1.0) if total else 1.0
    filled = round(width * ratio)
    return (
        f"[{'#' * filled}{'-' * (width - filled)}] {ratio * 100:5.1f}% "
        f"{completed}/{total} rows, {candidates} templates"
    )


def ensure_target_table(connection: pyodbc.Connection) -> None:
    table = qualified_table(TARGET_DB_CONFIG, TARGET_TABLE)
    with connection.cursor() as cursor:
        cursor.execute(
            f"""
            IF OBJECT_ID(N'{table}', N'U') IS NOT NULL
               AND (
                    COL_LENGTH(N'{table}', N'template_hash') IS NOT NULL
                    OR COL_LENGTH(N'{table}', N'page_id') IS NULL
                    OR COL_LENGTH(N'{table}', N'section_no') IS NULL
                    OR COL_LENGTH(N'{table}', N'paragraph_no') IS NULL
                    OR EXISTS (
                        SELECT 1
                        FROM sys.columns AS c
                        JOIN sys.types AS t ON c.user_type_id = t.user_type_id
                        WHERE c.object_id = OBJECT_ID(N'{table}')
                          AND c.name = N'template_text'
                          AND t.name <> N'nvarchar'
                    )
               )
                DROP TABLE {table};

            IF OBJECT_ID(N'{table}', N'U') IS NULL
            BEGIN
                CREATE TABLE {table} (
                    [page_id] BIGINT NOT NULL,
                    [section_no] INT NOT NULL,
                    [paragraph_no] INT NOT NULL,
                    [template_name] NVARCHAR(512) NULL,
                    [template_text] NVARCHAR(MAX) NOT NULL,
                    [created_at] DATETIME2 NOT NULL
                        CONSTRAINT [DF_template_created_at] DEFAULT SYSUTCDATETIME(),
                    CONSTRAINT [PK_template_location]
                        PRIMARY KEY ([page_id], [section_no], [paragraph_no])
                );
            END
            """
        )
    connection.commit()


def write_templates(
    connection: pyodbc.Connection,
    templates: dict[tuple[int, int, int], tuple[str, str]],
) -> int:
    """Insert or update a batch of templates keyed by source paragraph location."""
    if not templates:
        return 0
    table = qualified_table(TARGET_DB_CONFIG, TARGET_TABLE)
    statement = f"""
        MERGE {table} AS target
        USING (VALUES (?, ?, ?, ?, ?)) AS source
            ([page_id], [section_no], [paragraph_no], [template_name], [template_text])
          ON target.[page_id] = source.[page_id]
         AND target.[section_no] = source.[section_no]
         AND target.[paragraph_no] = source.[paragraph_no]
        WHEN MATCHED THEN
          UPDATE SET
              [template_name] = source.[template_name],
              [template_text] = source.[template_text]
        WHEN NOT MATCHED THEN
          INSERT (
              [page_id], [section_no], [paragraph_no], [template_name], [template_text]
          )
          VALUES (
              source.[page_id], source.[section_no], source.[paragraph_no],
              source.[template_name], source.[template_text]
          )
        OUTPUT $action;
    """
    inserted = 0
    with connection.cursor() as cursor:
        for (page_id, section_no, paragraph_no), (
            template_name,
            template_text,
        ) in templates.items():
            cursor.execute(
                statement,
                page_id,
                section_no,
                paragraph_no,
                template_name,
                template_text,
            )
            action = cursor.fetchone()
            inserted += int(action is not None and action[0] == "INSERT")
    connection.commit()
    return inserted


def copy_standalone_templates() -> tuple[int, int, int]:
    """Copy standalone templates and return source, candidate, and inserted counts."""
    source_table = qualified_table(SOURCE_DB_CONFIG, SOURCE_TABLE)
    source_rows = candidates = inserted = 0

    with connect(SOURCE_DB_CONFIG) as source_connection, connect(
        TARGET_DB_CONFIG
    ) as target_connection:
        ensure_target_table(target_connection)
        with source_connection.cursor() as source_cursor:
            total_source_rows = source_cursor.execute(
                f"SELECT COUNT_BIG(*) FROM {source_table} "
                "WHERE [wtp_input] IS NOT NULL"
            ).fetchone()[0]
            source_cursor.execute(
                f"SELECT [page_id], [section_no], [paragraph_no], [wtp_input] "
                f"FROM {source_table} WHERE [wtp_input] IS NOT NULL"
            )
            while rows := source_cursor.fetchmany(BATCH_SIZE):
                templates: dict[tuple[int, int, int], tuple[str, str]] = {}
                for page_id, section_no, paragraph_no, wtp_input in rows:
                    source_rows += 1
                    template = standalone_template(wtp_input)
                    if template is None:
                        continue
                    template_name, template_text = template
                    candidates += 1
                    templates[(page_id, section_no, paragraph_no)] = (
                        template_name,
                        template_text,
                    )
                inserted += write_templates(target_connection, templates)
                print(
                    "\r" + progress_line(source_rows, total_source_rows, candidates),
                    end="",
                    flush=True,
                )

    print()

    return source_rows, candidates, inserted


def main() -> None:
    source_rows, candidates, inserted = copy_standalone_templates()
    print(
        f"Completed. source_rows={source_rows}, "
        f"standalone_templates={candidates}, inserted={inserted}"
    )


if __name__ == "__main__":
    main()
