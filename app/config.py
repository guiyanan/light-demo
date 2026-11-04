"""配置加载（R1）。

- 密钥和接口地址从环境变量 / `.env` 读取
- 业务参数从 `config/app.yaml` 读取
- 缺少必填项时抛出 ConfigError，并指出缺的是哪个字段
- 任何对外展示（repr、safe_dict、日志）中密钥只显示前 4 位
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_YAML = ROOT / "config" / "app.yaml"

# 服务名 → 该服务需要的环境变量
SERVICE_ENV = {
    "xhs": ("XHS_BASE_URL", "XHS_API_KEY"),
    "llm": ("LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL"),
    "image": ("IMAGE_BASE_URL", "IMAGE_API_KEY", "IMAGE_MODEL"),
}
SERVICE_LABEL = {"xhs": "聚道云（小红书）", "llm": "Kimi（文本）", "image": "BananaRouter（生图）"}

# 服务名 → (地址变量, 是否要求以 /v1 结尾, 正确示例)（BUG-002）
SERVICE_URL = {
    "xhs": ("XHS_BASE_URL", False, "https://api.cn8n.com"),
    "llm": ("LLM_BASE_URL", True, "https://api.kimi.com/coding/v1"),
    "image": ("IMAGE_BASE_URL", True, "https://api.bananarouter.com/v1"),
}


class ConfigError(Exception):
    """配置缺失或格式错误。`missing` 为缺少的字段名列表。"""

    def __init__(self, message: str, missing: list[str] | None = None):
        super().__init__(message)
        self.missing = missing or []


def mask_secret(value: str | None) -> str:
    """密钥打码：只显示前 4 位；不足 8 位的全部隐藏。"""
    if not value:
        return ""
    if len(value) < 8:
        return "****"
    return value[:4] + "****"


@dataclass(frozen=True)
class CollectConfig:
    max_notes: int
    comments_per_note: int
    max_comments: int
    default_keywords: list[str]


@dataclass(frozen=True)
class HttpConfig:
    max_retries: int
    backoff_base_seconds: float


@dataclass(frozen=True)
class PathsConfig:
    data_dir: Path
    logs_dir: Path


@dataclass(frozen=True)
class ServiceConfig:
    base_url: str
    api_key: str = field(repr=False)
    timeout_seconds: float
    model: str = ""
    max_retries: int = 3
    temperature: float = 0.3
    size: str = "1024x1024"

    def __repr__(self) -> str:  # 密钥打码
        return (
            f"ServiceConfig(base_url={self.base_url!r}, api_key={mask_secret(self.api_key)!r}, "
            f"model={self.model!r}, timeout_seconds={self.timeout_seconds})"
        )


@dataclass(frozen=True)
class AppConfig:
    xhs: ServiceConfig
    llm: ServiceConfig
    image: ServiceConfig
    collect: CollectConfig
    http: HttpConfig
    paths: PathsConfig
    env_values: Mapping[str, str] = field(repr=False, default_factory=dict)

    def __repr__(self) -> str:
        return f"AppConfig({self.safe_dict()!r})"

    __str__ = __repr__

    def missing(self, *services: str) -> list[str]:
        """返回指定服务缺少的环境变量名（按声明顺序）。"""
        out: list[str] = []
        for svc in services:
            if svc not in SERVICE_ENV:
                raise ValueError(f"未知服务：{svc}")
            out += [k for k in SERVICE_ENV[svc] if not self.env_values.get(k, "").strip()]
        return out

    def invalid(self, *services: str) -> list[str]:
        """返回指定服务接口地址格式错误的说明（没填的地址不算，由 missing() 报告）。"""
        out: list[str] = []
        for svc in services:
            if svc not in SERVICE_URL:
                raise ValueError(f"未知服务：{svc}")
            key, need_v1, example = SERVICE_URL[svc]
            value = self.env_values.get(key, "").strip().rstrip("/")
            if not value:
                continue
            if not value.startswith(("http://", "https://")):
                out.append(f"{key} 应以 https:// 开头，例如 {example}（当前：{value}）")
            elif need_v1 and not value.endswith("/v1"):
                out.append(f"{key} 应填接口地址、以 /v1 结尾，例如 {example}（当前：{value}，像是网页地址）")
        return out

    def require(self, *services: str) -> None:
        missing = self.missing(*services)
        if missing:
            raise ConfigError(
                "缺少必填配置：" + "、".join(missing) + "（请在 .env 中填写后重新启动）", missing
            )
        invalid = self.invalid(*services)
        if invalid:
            raise ConfigError("接口地址填写有误：" + "；".join(invalid) + "（请在 .env 中改正后重新启动）")

    def secrets(self) -> list[str]:
        return [s.api_key for s in (self.xhs, self.llm, self.image) if s.api_key]

    def safe_dict(self) -> dict[str, Any]:
        """可写入日志的配置摘要（密钥已打码）。"""
        def svc(s: ServiceConfig) -> dict[str, Any]:
            return {
                "base_url": s.base_url,
                "api_key": mask_secret(s.api_key),
                "model": s.model,
                "timeout_seconds": s.timeout_seconds,
                "max_retries": s.max_retries,
            }

        return {
            "xhs": svc(self.xhs),
            "llm": svc(self.llm),
            "image": svc(self.image),
            "collect": {f.name: getattr(self.collect, f.name) for f in fields(self.collect)},
            "http": {f.name: getattr(self.http, f.name) for f in fields(self.http)},
        }


def _get(data: Mapping[str, Any], dotted: str) -> Any:
    cur: Any = data
    for part in dotted.split("."):
        if not isinstance(cur, Mapping) or part not in cur:
            raise ConfigError(f"config/app.yaml 缺少字段：{dotted}", [dotted])
        cur = cur[part]
    return cur


def _read_env(env: Mapping[str, str] | None) -> dict[str, str]:
    if env is not None:
        return {k: str(v) for k, v in env.items()}
    try:  # 本地开发时读 .env；Docker 中已由 compose 注入环境变量
        from dotenv import load_dotenv

        load_dotenv(ROOT / ".env", override=False)
    except ImportError:  # pragma: no cover
        pass
    keys = {k for ks in SERVICE_ENV.values() for k in ks}
    return {k: os.environ.get(k, "") for k in keys}


def load_config(env: Mapping[str, str] | None = None, yaml_path: Path | str | None = None) -> AppConfig:
    """加载配置。env 默认取进程环境变量（及 .env）；yaml_path 默认 config/app.yaml。

    业务参数缺失会立即报错；接口密钥缺失不在这里报错，由各服务调用 `require()` 时报错，
    这样某一个接口没填密钥时，其他接口仍然可以检查。
    """
    path = Path(yaml_path) if yaml_path else DEFAULT_YAML
    if not path.is_file():
        raise ConfigError(f"找不到配置文件：{path}", [str(path)])
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        raise ConfigError(f"配置文件格式错误：{path}：{e}") from e

    ev = _read_env(env)
    max_retries = int(_get(data, "http.max_retries"))

    def url(key: str) -> str:
        return ev.get(key, "").strip().rstrip("/")

    return AppConfig(
        xhs=ServiceConfig(
            base_url=url("XHS_BASE_URL"),
            api_key=ev.get("XHS_API_KEY", "").strip(),
            timeout_seconds=float(_get(data, "xhs.timeout_seconds")),
            max_retries=max_retries,
        ),
        llm=ServiceConfig(
            base_url=url("LLM_BASE_URL"),
            api_key=ev.get("LLM_API_KEY", "").strip(),
            model=ev.get("LLM_MODEL", "").strip(),
            timeout_seconds=float(_get(data, "llm.timeout_seconds")),
            temperature=float(_get(data, "llm.temperature")),
            max_retries=max_retries,
        ),
        image=ServiceConfig(
            base_url=url("IMAGE_BASE_URL"),
            api_key=ev.get("IMAGE_API_KEY", "").strip(),
            model=ev.get("IMAGE_MODEL", "").strip(),
            timeout_seconds=float(_get(data, "image.timeout_seconds")),
            max_retries=int(_get(data, "image.max_retries")),
            size=str(_get(data, "image.size")),
        ),
        collect=CollectConfig(
            max_notes=int(_get(data, "collect.max_notes")),
            comments_per_note=int(_get(data, "collect.comments_per_note")),
            max_comments=int(_get(data, "collect.max_comments")),
            default_keywords=list(_get(data, "collect.default_keywords")),
        ),
        http=HttpConfig(
            max_retries=max_retries,
            backoff_base_seconds=float(_get(data, "http.backoff_base_seconds")),
        ),
        paths=PathsConfig(
            data_dir=Path(_get(data, "paths.data_dir")),
            logs_dir=Path(_get(data, "paths.logs_dir")),
        ),
        env_values=ev,
    )
