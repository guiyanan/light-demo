"""Step 1：项目骨架（对应 R17、S7）。

这些测试只读仓库里的文件，不依赖网络、Docker 或当前时间。
"""

from __future__ import annotations

import importlib
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]

REQUIRED_ENV_KEYS = [
    "XHS_BASE_URL",
    "XHS_API_KEY",
    "LLM_BASE_URL",
    "LLM_API_KEY",
    "LLM_MODEL",
    "IMAGE_BASE_URL",
    "IMAGE_API_KEY",
    "IMAGE_MODEL",
]


def _gitignore_patterns() -> list[str]:
    lines = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    return [ln.strip() for ln in lines if ln.strip() and not ln.strip().startswith("#")]


def test_app_importable():
    mod = importlib.import_module("app")
    assert hasattr(mod, "__version__")
    assert (ROOT / "app" / "ui" / "streamlit_app.py").is_file()


def test_gitignore_excludes_secrets_and_runtime():
    patterns = _gitignore_patterns()
    for needed in (".env", "logs/", "data/runs/", "data/cache/"):
        assert needed in patterns, f".gitignore 缺少 {needed}"
    # .env.example 必须能提交
    assert "!.env.example" in patterns


def test_env_example_has_all_keys():
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    keys = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        keys[k.strip()] = v.strip()
    for k in REQUIRED_ENV_KEYS:
        assert k in keys, f".env.example 缺少 {k}"
    # 样例文件里密钥必须是空的
    for k in ("XHS_API_KEY", "LLM_API_KEY", "IMAGE_API_KEY"):
        assert keys[k] == "", f"{k} 在 .env.example 中必须留空"


def test_start_command_runs_pull_and_compose():
    path = ROOT / "start.command"
    text = path.read_text(encoding="utf-8")
    assert text.startswith("#!/bin/bash")
    assert "git pull" in text
    assert "docker compose up --build" in text
    assert text.index("git pull") < text.index("docker compose up --build")
    # 构建输出要写到 logs/build/
    assert "logs/build" in text
    # 必须是可执行文件（Mac 上双击才能运行）
    assert path.stat().st_mode & 0o111, "start.command 需要可执行权限"


def test_compose_mounts_data_and_logs():
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    service = next(iter(compose["services"].values()))
    volumes = service.get("volumes", [])
    targets = {v.split(":")[1] for v in volumes if isinstance(v, str)}
    assert "/app/data" in targets
    assert "/app/logs" in targets
    assert "8501:8501" in [str(p) for p in service.get("ports", [])]
    # .env 通过 env_file 注入，且不存在时 build 不报错（CI 里没有 .env）
    env_files = service["env_file"]
    assert any(isinstance(e, dict) and e.get("path") == ".env" for e in env_files)


def test_dockerfile_installs_cjk_font():
    text = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "fonts-noto-cjk" in text
    assert "python:3.11" in text
