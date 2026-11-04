"""接口检查（R14）：依次测试聚道云、Kimi、生图是否连通。

用法：
    python -m app.diag                 # 检查全部 3 个接口
    python -m app.diag --only xhs      # 只检查一个
网页「接口检查」页调用同一个 run_check()。

每次检查：显示成功 / 失败、状态码、耗时、编号（request_id）；
脱敏后的原始响应保存到 logs/diag/，用于确认真实数据结构（第 2、5 轮据此写解析代码）。
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import sys
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from app.clients.http import ApiError, CallCounter, HttpResult
from app.clients.image import ImageClient, image_ext
from app.clients.llm import LlmClient
from app.clients.xhs import COMMENTS_PATH, SEARCH_PATH, XhsClient, business_error, find_first_note
from app.config import ROOT, SERVICE_LABEL, AppConfig, ConfigError, load_config
from app.logging_setup import current_log_dir, redact, register_secrets, request_context, setup_logging

logger = logging.getLogger(__name__)

SERVICES = SERVICE_LABEL  # {"xhs": ..., "llm": ..., "image": ...}，顺序即检查顺序
DEFAULT_KEYWORD = "低温酸奶"
DIAG_IMAGE_PROMPT = (
    "一盒简约的白色低温酸奶杯，放在浅色桌面上，柔和自然光，产品摄影风格。包装上不要出现任何文字、字母、数字和 logo。"
)
LONG_STRING = 2000


@dataclass
class CheckResult:
    service: str
    label: str
    ok: bool
    status_code: int | None
    elapsed_ms: int
    message: str
    request_id: str
    error_type: str | None = None
    raw_files: list[str] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------- 脱敏保存 ----------------
def mask_raw(obj: Any, key: str = "") -> Any:
    """递归脱敏：字符串中的密钥打码；base64 等超长字符串只保留长度说明。"""
    if isinstance(obj, dict):
        return {k: mask_raw(v, k) for k, v in obj.items()}
    if isinstance(obj, list):
        return [mask_raw(v, key) for v in obj]
    if isinstance(obj, str):
        if key == "b64_json" or len(obj) > LONG_STRING:
            return f"<base64/长文本 {len(obj)} 字符，已省略>"
        return redact(obj)
    return obj


class _Saver:
    def __init__(self, diag_dir: Path, now: dt.datetime, request_id: str):
        self.dir = diag_dir
        self.prefix = now.strftime("%Y%m%d-%H%M%S")
        self.request_id = request_id
        self.files: list[str] = []

    def _path(self, name: str, ext: str) -> Path:
        self.dir.mkdir(parents=True, exist_ok=True)
        return self.dir / f"{self.prefix}-{name}-{self.request_id}.{ext}"

    def json(self, name: str, *, method: str, url: str, status_code: int | None, elapsed_ms: int | None,
             response: Any = None, error: ApiError | None = None) -> None:
        payload: dict[str, Any] = {
            "request_id": self.request_id,
            "request": {"method": method, "url": redact(url)},
            "status_code": status_code,
            "elapsed_ms": elapsed_ms,
        }
        if error is not None:
            payload["error"] = {"type": error.error_type, "message": str(error), "body_excerpt": error.body}
        else:
            payload["response"] = mask_raw(response)
        p = self._path(name, "json")
        p.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        self.files.append(str(p))

    def binary(self, name: str, content: bytes, ext: str) -> None:
        p = self._path(name, ext)
        p.write_bytes(content)
        self.files.append(str(p))

    def from_result(self, name: str, method: str, res: HttpResult) -> None:
        self.json(name, method=method, url=res.url, status_code=res.status_code, elapsed_ms=res.elapsed_ms,
                  response=res.data)


# ---------------- 三个检查 ----------------
def _check_xhs(cfg: AppConfig, saver: _Saver, *, sleep: Callable, counter: CallCounter | None,
               keyword: str, **_: Any) -> tuple[bool, int | None, str, dict[str, Any]]:
    client = XhsClient(cfg, sleep=sleep, counter=counter)
    try:
        res = client.search_notes_raw(keyword)
    except ApiError as e:
        saver.json("xhs-search", method="GET", url=cfg.xhs.base_url + SEARCH_PATH, status_code=e.status_code,
                   elapsed_ms=None, error=e)
        raise
    saver.from_result("xhs-search", "GET", res)
    if err := business_error(res.data):
        return False, res.status_code, f"搜索接口：{err}", {}
    details: dict[str, Any] = {"keyword": keyword, "search_status": res.status_code}
    note = find_first_note(res.data)
    if not note:
        return True, res.status_code, "搜索成功，但没有找到 note_id，评论接口未检查（原始响应已保存）", details
    details["note_id"] = note["note_id"]
    try:
        cres = client.get_note_comments_raw(note["note_id"], xsec_token=note["xsec_token"] or None)
    except ApiError as e:
        saver.json("xhs-comments", method="GET", url=cfg.xhs.base_url + COMMENTS_PATH, status_code=e.status_code,
                   elapsed_ms=None, error=e)
        details["comments_status"] = e.status_code
        return False, e.status_code, f"搜索成功，但获取评论失败：{e.summary()}", details
    saver.from_result("xhs-comments", "GET", cres)
    details["comments_status"] = cres.status_code
    if err := business_error(cres.data):
        return False, cres.status_code, f"评论接口：{err}", details
    return True, res.status_code, "搜索和评论接口均连通", details


def _check_llm(cfg: AppConfig, saver: _Saver, *, sleep: Callable, counter: CallCounter | None,
               llm_client: Any = None, **_: Any) -> tuple[bool, int | None, str, dict[str, Any]]:
    client = LlmClient(cfg, client=llm_client, sleep=sleep, counter=counter)
    try:
        res = client.chat([{"role": "user", "content": "请只回复两个字母：OK"}], max_tokens=200, operation="diag")
    except ApiError as e:
        saver.json("llm-chat", method="POST", url=client.url, status_code=e.status_code, elapsed_ms=None, error=e)
        raise
    saver.json("llm-chat", method="POST", url=client.url, status_code=200, elapsed_ms=res.elapsed_ms,
               response=res.raw)
    details = {"model": res.model, "reply": res.content[:100], "attempts": res.attempts}
    return True, 200, f"模型 {res.model} 已回复：{res.content[:30] or '（空）'}", details


def _check_image(cfg: AppConfig, saver: _Saver, *, sleep: Callable, counter: CallCounter | None,
                 **_: Any) -> tuple[bool, int | None, str, dict[str, Any]]:
    client = ImageClient(cfg, sleep=sleep, counter=counter)
    url = f"{cfg.image.base_url}/images/generations"
    try:
        res = client.generate_raw(DIAG_IMAGE_PROMPT)
    except ApiError as e:
        saver.json("image-generate", method="POST", url=url, status_code=e.status_code, elapsed_ms=None, error=e)
        raise
    saver.from_result("image-generate", "POST", res)
    fmt, content = client.extract_image(res.data)
    saver.binary("image-sample", content, image_ext(content))
    details = {"image_format": fmt, "image_bytes": len(content), "attempts": res.attempts}
    return True, res.status_code, f"生图成功（返回格式 {fmt}，{len(content) // 1024} KB），测试图已保存", details


CHECKS = {"xhs": _check_xhs, "llm": _check_llm, "image": _check_image}


# ---------------- 入口 ----------------
def ensure_logging(cfg: AppConfig | None) -> None:
    """若尚未初始化日志则按配置初始化（测试或网页已初始化时不覆盖）。"""
    if current_log_dir() is None:
        logs_dir = cfg.paths.logs_dir if cfg else Path("logs")
        setup_logging(logs_dir if logs_dir.is_absolute() else ROOT / logs_dir)
    if cfg:
        register_secrets(cfg.secrets())


def run_check(
    service: str,
    *,
    cfg: AppConfig | None = None,
    diag_dir: Path | None = None,
    now: dt.datetime | None = None,
    sleep: Callable[[float], Any] = time.sleep,
    counter: CallCounter | None = None,
    keyword: str = DEFAULT_KEYWORD,
    llm_client: Any = None,
) -> CheckResult:
    """检查一个接口，任何错误都转为失败结果返回（不抛异常）。"""
    if service not in CHECKS:
        raise ValueError(f"未知接口：{service}")
    label = SERVICES[service]
    start = time.perf_counter()
    with request_context() as rid:
        def result(ok: bool, code: int | None, msg: str, **kw: Any) -> CheckResult:
            r = CheckResult(service, label, ok, code, round((time.perf_counter() - start) * 1000),
                            redact(msg), rid, **kw)
            logger.log(logging.INFO if ok else logging.ERROR, f"接口检查 {label}：{'成功' if ok else '失败'}",
                       extra={"event": "diag", "service": service, "status_code": code, "ok": ok,
                              "elapsed_ms": r.elapsed_ms, "error_type": r.error_type, "diag_message": r.message})
            return r

        try:
            cfg = cfg or load_config()
        except ConfigError as e:
            ensure_logging(None)
            return result(False, None, str(e), error_type="config_missing")
        ensure_logging(cfg)
        missing = cfg.missing(service)
        if missing:
            return result(False, None, f"缺少必填配置：{'、'.join(missing)}（请在 .env 中填写后重新启动）",
                          error_type="config_missing")
        invalid = cfg.invalid(service)
        if invalid:
            return result(False, None, "接口地址填写有误：" + "；".join(invalid) + "（请在 .env 中改正后重新启动）",
                          error_type="config_invalid")
        logs_dir = cfg.paths.logs_dir if cfg.paths.logs_dir.is_absolute() else ROOT / cfg.paths.logs_dir
        saver = _Saver(diag_dir or logs_dir / "diag", now or dt.datetime.now(), rid)
        try:
            ok, code, msg, details = CHECKS[service](
                cfg, saver, sleep=sleep, counter=counter, keyword=keyword, llm_client=llm_client
            )
            return result(ok, code, msg, raw_files=saver.files, details=details,
                          error_type=None if ok else "business_error")
        except ApiError as e:
            return result(False, e.status_code, e.summary(), error_type=e.error_type, raw_files=saver.files)
        except Exception as e:  # 兜底：任何意外错误都记录堆栈并显示
            logger.exception(f"接口检查 {label} 出现意外错误")
            return result(False, None, f"意外错误：{type(e).__name__}: {e}", error_type="unexpected",
                          raw_files=saver.files)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.diag", description="接口检查：聚道云、Kimi、生图")
    parser.add_argument("--only", choices=list(SERVICES), help="只检查一个接口")
    parser.add_argument("--keyword", default=DEFAULT_KEYWORD, help="聚道云搜索用的关键词")
    args = parser.parse_args(argv)
    services = [args.only] if args.only else list(SERVICES)
    all_ok = True
    print("接口检查结果")
    print("-" * 60)
    for svc in services:
        r = run_check(svc, keyword=args.keyword)
        all_ok &= r.ok
        status = "✅ 成功" if r.ok else "❌ 失败"
        print(f"{r.label}  {status}  状态码 {r.status_code if r.status_code is not None else '-'}"
              f"  耗时 {r.elapsed_ms}ms  编号 {r.request_id}")
        print(f"    {r.message}")
        for f in r.raw_files:
            print(f"    原始响应：{f}")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
