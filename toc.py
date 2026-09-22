# -*- coding: utf-8 -*-

import pyodbc


DB_CONFIG = {
    "host": "localhost",
    "port": 1433,
    "user": "sa",
    "password": "Wang342688",
    "database": "Dev0919",
    "schema": "dbo",
}

TABLE_NAME = "wiki_sections_[20260801]"

OUTPUT_FILE = "toc_result.txt"


KNOWN_TOCS = [
    "references",
    "other websites",
    "related pages",
    "more reading",
    "category",
    "external links",
    "footnotes",
    "notes",
    "See also"
]


def get_connection():
    """连接 SQL Server"""

    conn_str = (
        "DRIVER={ODBC Driver 17 for SQL Server};"
        f"SERVER={DB_CONFIG['host']},{DB_CONFIG['port']};"
        f"DATABASE={DB_CONFIG['database']};"
        f"UID={DB_CONFIG['user']};"
        f"PWD={DB_CONFIG['password']};"
        "TrustServerCertificate=yes;"
    )

    return pyodbc.connect(conn_str)


def quote_identifier(name):
    """SQL Server 标识符转义"""
    return "[" + name.replace("]", "]]") + "]"


def find_candidate_tocs():

    schema = quote_identifier(DB_CONFIG["schema"])
    table = quote_identifier(TABLE_NAME)

    full_table_name = f"{schema}.{table}"

    placeholders = ", ".join("?" for _ in KNOWN_TOCS)

    sql = f"""
    WITH ordered_data AS (
        SELECT
            id,
            page_id,
            section_no,
            toc,

            ROW_NUMBER() OVER (
                PARTITION BY page_id
                ORDER BY section_no, id
            ) AS rn

        FROM {full_table_name}
    ),

    first_known AS (
        SELECT
            page_id,
            MIN(rn) AS start_rn

        FROM ordered_data

        WHERE LOWER(LTRIM(RTRIM(toc))) IN ({placeholders})

        GROUP BY page_id
    ),

    candidate_rows AS (
        SELECT
            o.page_id,
            o.section_no,
            o.toc,
            o.rn,
            f.start_rn

        FROM ordered_data o

        INNER JOIN first_known f
            ON o.page_id = f.page_id

        -- 前 0 个：
        -- 直接从第一个已明确 TOC 开始，
        -- 获取该位置及之后全部数据
        WHERE o.rn >= f.start_rn
    )

    SELECT DISTINCT
        LTRIM(RTRIM(toc)) AS toc

    FROM candidate_rows

    WHERE
        toc IS NOT NULL

        AND LTRIM(RTRIM(toc)) <> ''

        -- 去掉所有已明确 TOC
        AND LOWER(LTRIM(RTRIM(toc))) NOT IN ({placeholders})

    ORDER BY toc;
    """

    # SQL 中使用了两次 KNOWN_TOCS
    params = KNOWN_TOCS + KNOWN_TOCS

    print("=" * 70)
    print("开始查询")
    print(f"数据库：{DB_CONFIG['database']}")
    print(f"数据表：{TABLE_NAME}")
    print(f"输出文件：{OUTPUT_FILE}")
    print("=" * 70)

    conn = get_connection()

    try:
        cursor = conn.cursor()

        cursor.execute(sql, params)

        rows = cursor.fetchall()

        result = [
            row[0]
            for row in rows
            if row[0]
        ]

        print(f"\n查询完成。")
        print(f"去重后待确认 TOC 数量：{len(result):,}")

        # 保存 TXT
        with open(
            OUTPUT_FILE,
            "w",
            encoding="utf-8"
        ) as f:

            for toc in result:
                f.write(toc + "\n")

        print(f"结果已保存：{OUTPUT_FILE}")

        return result

    finally:
        conn.close()


if __name__ == "__main__":
    find_candidate_tocs()
