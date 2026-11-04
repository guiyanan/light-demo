"""公共测试夹具。"""

from __future__ import annotations

import logging

import pytest


@pytest.fixture()
def log_dir(tmp_path):
    """把项目日志写到临时目录，测试结束后还原，避免测试之间互相影响。"""
    from app import logging_setup

    logging_setup.setup_logging(tmp_path, force=True, console=False)
    yield tmp_path
    logging_setup.reset_logging()


def read_json_lines(path):
    import json

    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


@pytest.fixture()
def read_logs(log_dir):
    def _read(category: str = "app"):
        for h in logging.getLogger("app").handlers:
            h.flush()
        return read_json_lines(log_dir / category / f"{category}.log")

    return _read
