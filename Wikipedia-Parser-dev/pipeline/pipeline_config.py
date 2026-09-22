"""加载解析器 CLI 所需的单文件配置文件。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from wtp_integration import load_wtp_settings


@dataclass(frozen=True)
class PipelineConfig:
    """解析器 SQL 和 WTP 部分所需的已解析设置。"""

    sqlserver: dict[str, Any]
    wtp: dict[str, Any]
    dump_file: Path | None


@dataclass(frozen=True)
class OutputTableNames:
    """一次 XML 导入期间写入 SQL Server 的所有表。"""

    raw: str
    processed: str
    component_prefix: str
    suffix: str
    sections: str
    paragraphs: str
    wtp_intermediate: str
    sentences: str
    checkpoint: str

    def component_table_name(self, component_type: str) -> str:
        """返回一个组件输出表的物理名称。"""
        return f"{self.component_prefix}_{component_type}{self.suffix}"


def _resolve_path(value: str, base: Path) -> Path:
    path = Path(value)
    return (base / path).resolve() if not path.is_absolute() else path.resolve()


def _as_object(value: object, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"统一配置必须在 {field} 处包含一个对象")
    return value


def _with_wiki_prefix(table_name: str) -> str:
    """将一个输出基础名称规范化为所需的 ``wiki_`` 前缀。"""
    return table_name if table_name.startswith("wiki_") else f"wiki_{table_name}"


def _validate_sqlserver(config: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(config)
    required = ("host", "user", "password", "database")
    missing = [key for key in required if not result.get(key)]
    if missing:
        raise ValueError(
            f"缺少 sqlserver 配置键：{', '.join(missing)}"
        )
    return result


def load_pipeline_config(config_path: Path) -> PipelineConfig:
    """加载包含源、SQL Server 和 WTP 设置的配置。"""
    with config_path.open("r", encoding="utf-8") as f:
        config = _as_object(json.load(f), "根节点")

    sqlserver = _validate_sqlserver(_as_object(config.get("sqlserver"), "sqlserver"))
    wtp = load_wtp_settings(
        _as_object(config.get("wtp"), "wtp"), config_path.parent
    )
    source = _as_object(config.get("source", {}), "source")
    dump_value = source.get("dump_file")
    dump_file = (
        _resolve_path(str(dump_value), config_path.parent)
        if dump_value else None
    )
    return PipelineConfig(sqlserver=sqlserver, wtp=wtp, dump_file=dump_file)


def resolve_dump_file(
    *,
    cli_dump_file: Path | None,
    source_table: str | None,
    configured_dump_file: Path | None,
) -> Path | None:
    """根据 CLI 覆盖和 SQL 源排除选择 XML 输入。"""
    if source_table is not None:
        return None
    return cli_dump_file or configured_dump_file


def output_table_names_for_dump(
    dump_file: Path,
    *,
    raw_table: str,
    processed_table: str,
    component_prefix: str,
    sections_table: str,
    paragraphs_table: str,
    wtp_intermediate_table: str,
    sentences_table: str,
    checkpoint_table: str,
) -> OutputTableNames:
    """为 XML 输出表添加转储的 ``_[YYYYMMDD]`` 日期后缀。"""
    stem = dump_file.name
    if stem.endswith(".bz2"):
        stem = stem.removesuffix(".bz2")
    if stem.endswith(".xml"):
        stem = stem.removesuffix(".xml")
    match = re.search(r"-(?P<date>\d{8})(?:-|$)", stem)
    if match is None:
        raise ValueError(
            "XML 转储文件名必须包含独立的八位数字日期，"
            "例如 simplewiki-20260801-pages-articles.xml.bz2。"
        )
    suffix = f"_[{match.group('date')}]"
    return OutputTableNames(
        raw=_with_wiki_prefix(raw_table) + suffix,
        processed=_with_wiki_prefix(processed_table) + suffix,
        component_prefix=_with_wiki_prefix(component_prefix),
        suffix=suffix,
        sections=_with_wiki_prefix(sections_table) + suffix,
        paragraphs=_with_wiki_prefix(paragraphs_table) + suffix,
        wtp_intermediate=_with_wiki_prefix(wtp_intermediate_table) + suffix,
        sentences=_with_wiki_prefix(sentences_table) + suffix,
        checkpoint=_with_wiki_prefix(checkpoint_table) + suffix,
    )
