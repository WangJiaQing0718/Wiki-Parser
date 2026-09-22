"""每运行一次、仅解析失败的日志文件。"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TextIO


@dataclass
class RunLogSession:
    """拥有自己运行的失败日志并立即刷新每一行失败日志。"""

    path: Path
    _log_file: TextIO
    _lock: threading.Lock

    def write_parse_failure(self, line: str) -> None:
        """追加恰好一行控制台格式的解析失败行。"""
        with self._lock:
            self._log_file.write(line.rstrip("\r\n") + "\n")
            self._log_file.flush()

    def close(self) -> None:
        self._log_file.close()


def start_run_log(
    log_dir: Path,
    *,
    now: datetime | None = None,
    pid: int | None = None,
) -> RunLogSession:
    """为一运行创建唯一的、初始为空的解析失败日志。"""
    log_dir.mkdir(parents=True, exist_ok=True)
    now = now or datetime.now()
    pid = os.getpid() if pid is None else pid
    filename = f"parser-{now:%Y%m%d-%H%M%S}-{pid}"
    suffix = 0
    while True:
        name = f"{filename}.log" if suffix == 0 else f"{filename}-{suffix}.log"
        path = log_dir / name
        try:
            log_file = path.open("x", encoding="utf-8", buffering=1)
            break
        except FileExistsError:
            suffix += 1
    return RunLogSession(path=path, _log_file=log_file, _lock=threading.Lock())
