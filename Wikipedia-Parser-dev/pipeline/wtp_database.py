"""为 Wikipedia XML 转储准备本地 WTP SQLite 数据库。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4


WTP_NAMESPACE_IDS = {0, 10, 828}


def wtp_database_path_for_dump(dump_path: Path) -> Path:
    """返回与 XML 转储同目录、同版本的 WTP 数据库路径。"""
    name = dump_path.name
    if not name.endswith(".xml.bz2"):
        raise ValueError(f"WTP database requires a .xml.bz2 dump: {dump_path}")
    stem = name.removesuffix(".xml.bz2")
    return dump_path.with_name(f"{stem}-wtp-full.db")


def build_wtp_database(
    path: Path,
    db_path: Path,
    *,
    namespace_ids: set[int],
    lang_code: str = "en",
    project: str = "wikipedia",
) -> None:
    """将指定命名空间从转储导入一个新的 WTP SQLite 数据库。"""
    if db_path.exists():
        raise FileExistsError(f"Refusing to overwrite WTP database: {db_path}")

    from wikitextprocessor import Wtp
    from wikitextprocessor.dumpparser import process_dump

    temporary_path = db_path.with_name(f".{db_path.name}.{uuid4().hex}.tmp")
    wtp = Wtp(db_path=temporary_path, lang_code=lang_code, project=project)
    try:
        try:
            process_dump(wtp, str(path), namespace_ids)
        finally:
            wtp.close_db_conn()
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise
    try:
        if db_path.exists():
            raise FileExistsError(f"Refusing to overwrite WTP database: {db_path}")
        temporary_path.replace(db_path)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


def ensure_wtp_database(dump_path: Path, settings: Mapping[str, Any]) -> Path:
    """复用匹配数据库，或从 XML 转储创建缺失的数据库。"""
    dump_path = dump_path.resolve()
    expected_path = wtp_database_path_for_dump(dump_path)
    if expected_path.is_file():
        return expected_path

    print(f"WTP database not found; building {expected_path} from {dump_path}")
    build_wtp_database(
        dump_path,
        expected_path,
        namespace_ids=WTP_NAMESPACE_IDS,
        lang_code=str(settings.get("lang_code", "en")),
        project=str(settings.get("project", "wikipedia")),
    )
    if not expected_path.is_file():
        raise RuntimeError(f"WTP database builder did not create: {expected_path}")
    return expected_path
