"""Load the required single-file configuration used by the parser CLI."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from wtp_integration import load_wtp_settings


@dataclass(frozen=True)
class PipelineConfig:
    """Resolved settings needed by the SQL and WTP sides of the pipeline."""

    sqlserver: dict[str, Any]
    wtp: dict[str, Any]
    dump_file: Path | None


def _resolve_path(value: str, base: Path) -> Path:
    path = Path(value)
    return (base / path).resolve() if not path.is_absolute() else path.resolve()


def _as_object(value: object, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"Unified config must contain an object at {field}")
    return value


def _validate_sqlserver(config: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(config)
    required = ("host", "user", "password", "database")
    missing = [key for key in required if not result.get(key)]
    if missing:
        raise ValueError(
            f"Missing sqlserver config keys: {', '.join(missing)}"
        )
    return result


def load_pipeline_config(config_path: Path) -> PipelineConfig:
    """Load one config containing source, SQL Server, and WTP settings."""
    with config_path.open("r", encoding="utf-8") as f:
        config = _as_object(json.load(f), "the root")

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
    """Select XML input with CLI overrides and SQL-source exclusion."""
    if source_table is not None:
        return None
    return cli_dump_file or configured_dump_file
