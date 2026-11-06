"""运行记录保存与读取（R15）。

每次运行保存到 data/runs/<run_id>/：
    raw/NNN_<kind>.json   已去除用户身份信息的原始响应
    notes.jsonl           笔记（每行一篇）
    comments.jsonl        评论（每行一条）
    stats.json            关键词、条数、调用次数、消耗点数、创建时间、已完成的步骤
    insight.json          ②需求洞察结果（需求 + 原话证据 + 统计）
    concepts.json         ③新品概念结果（3 个概念 + 统计 / 提示）
读取历史运行只读本地文件，不调用任何接口。后续步骤（需求、概念、图片）在同一目录追加文件。
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
import time
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.clients.http import CallCounter
from app.clients.llm import LlmClient
from app.clients.xhs import XhsClient
from app.config import ROOT, AppConfig, load_config
from app.pipeline import collect as collect_mod
from app.pipeline import concept as concept_mod
from app.pipeline import insight as insight_mod
from app.pipeline.collect import CollectResult

logger = logging.getLogger(__name__)

_RUN_ID_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z_-]*$")


class RunNotFound(Exception):
    """运行记录不存在或编号不合法。"""


def runs_dir(cfg: AppConfig) -> Path:
    base = cfg.paths.data_dir
    return (base if base.is_absolute() else ROOT / base) / "runs"


def _write_json(path: Path, obj: Any) -> None:
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def save_collect(
    result: CollectResult, base_dir: Path, *, now: dt.datetime | None = None, suffix: str | None = None
) -> str:
    """保存一次数据抓取结果，返回 run_id（时间 + 随机后缀，按名字排序即按时间排序）。"""
    now = now or dt.datetime.now(dt.UTC)
    run_id = f"{now.strftime('%Y%m%d-%H%M%S')}-{suffix or uuid.uuid4().hex[:4]}"
    d = Path(base_dir) / run_id
    try:
        (d / "raw").mkdir(parents=True, exist_ok=False)
        for i, item in enumerate(result.raw, start=1):
            _write_json(d / "raw" / f"{i:03d}_{item.get('kind', 'raw')}.json", item)
        _write_jsonl(d / "notes.jsonl", result.notes)
        _write_jsonl(d / "comments.jsonl", result.comments)
        stats = {"run_id": run_id, "created_at": now.isoformat(timespec="seconds"), "steps": ["collect"],
                 **result.stats}
        _write_json(d / "stats.json", stats)
    except OSError:
        logger.exception("运行记录保存失败", extra={"event": "run_save_failed", "run_id": run_id, "path": str(d)})
        raise
    logger.info("运行记录已保存", extra={"event": "run_saved", "run_id": run_id, "path": str(d)})
    return run_id


@dataclass
class RunSummary:
    run_id: str
    created_at: str
    keywords: list[str]
    notes: int
    comments: int

    def label(self) -> str:
        """下拉框里显示的文字，例如：2026-11-04 21:30 · 低温酸奶 · 10 篇 / 20 条评论"""
        when = self.created_at[:16].replace("T", " ")
        return f"{when} · {'、'.join(self.keywords)} · {self.notes} 篇 / {self.comments} 条评论"


@dataclass
class Run:
    run_id: str
    path: Path
    notes: list[dict[str, Any]]
    comments: list[dict[str, Any]]
    stats: dict[str, Any]

    def raw_files(self) -> list[str]:
        raw = self.path / "raw"
        return sorted(p.name for p in raw.iterdir()) if raw.is_dir() else []


def list_runs(base_dir: Path) -> list[RunSummary]:
    """列出历史运行，最新的在前；损坏的目录跳过并记日志。"""
    base_dir = Path(base_dir)
    if not base_dir.is_dir():
        return []
    out: list[RunSummary] = []
    for d in base_dir.iterdir():
        if not d.is_dir():
            continue
        try:
            s = json.loads((d / "stats.json").read_text(encoding="utf-8"))
            out.append(
                RunSummary(
                    run_id=d.name,
                    created_at=str(s.get("created_at", "")),
                    keywords=list(s.get("keywords", [])),
                    notes=int(s.get("notes", 0)),
                    comments=int(s.get("comments", 0)),
                )
            )
        except (OSError, ValueError, TypeError) as e:
            logger.warning(
                f"跳过无法读取的运行记录：{d.name}（{e}）",
                extra={"event": "run_skip", "run_id": d.name, "error_type": type(e).__name__},
            )
    out.sort(key=lambda r: (r.created_at, r.run_id), reverse=True)
    return out


def _run_path(base_dir: Path, run_id: str) -> Path:
    if not run_id or not _RUN_ID_RE.match(run_id):
        raise RunNotFound(f"运行编号不合法：{run_id!r}")
    return Path(base_dir) / run_id


def load_run(base_dir: Path, run_id: str) -> Run:
    """读取一次历史运行（只读本地文件，不调用接口）。"""
    d = _run_path(base_dir, run_id)
    stats_path = d / "stats.json"
    if not stats_path.is_file():
        raise RunNotFound(f"找不到运行记录：{run_id}")
    try:
        stats = json.loads(stats_path.read_text(encoding="utf-8"))
        notes = _read_jsonl(d / "notes.jsonl")
        comments = _read_jsonl(d / "comments.jsonl")
    except (OSError, ValueError) as e:
        logger.exception("运行记录读取失败", extra={"event": "run_load_failed", "run_id": run_id})
        raise RunNotFound(f"运行记录已损坏：{run_id}（{e}）") from e
    logger.info("读取历史运行", extra={"event": "run_loaded", "run_id": run_id, "count": len(notes)})
    return Run(run_id=run_id, path=d, notes=notes, comments=comments, stats=stats)


def collect_and_save(
    keywords: Iterable[str],
    cfg: AppConfig | None = None,
    *,
    base_dir: Path | None = None,
    now: dt.datetime | None = None,
    sleep: Callable[[float], Any] = time.sleep,
) -> str:
    """网页「开始抓取」：检查配置 → 抓取 → 保存，返回 run_id。每次运行单独统计调用次数。"""
    cfg = cfg or load_config()
    cfg.require("xhs")
    client = XhsClient(cfg, sleep=sleep, counter=CallCounter())
    result = collect_mod.collect(client, keywords, cfg.collect)
    return save_collect(result, Path(base_dir) if base_dir else runs_dir(cfg), now=now)


# ---------------- ②需求洞察 ----------------
def cache_dir(cfg: AppConfig) -> Path:
    base = cfg.paths.data_dir
    return (base if base.is_absolute() else ROOT / base) / "cache" / "llm"


def _save_step(base_dir: Path, run_id: str, filename: str, step: str, data: dict[str, Any]) -> None:
    """把某一步的结果写入 <run_id>/<filename>，并在 stats.json 的 steps 里记一笔（重做不重复记）。"""
    d = _run_path(base_dir, run_id)
    stats_path = d / "stats.json"
    if not stats_path.is_file():
        raise RunNotFound(f"找不到运行记录：{run_id}")
    try:
        _write_json(d / filename, data)
        stats = json.loads(stats_path.read_text(encoding="utf-8"))
        if step not in stats.setdefault("steps", []):
            stats["steps"].append(step)
        _write_json(stats_path, stats)
    except (OSError, ValueError):
        logger.exception(f"{step} 结果保存失败", extra={"event": f"{step}_save_failed", "run_id": run_id})
        raise
    logger.info(f"{step} 结果已保存", extra={"event": f"{step}_saved", "run_id": run_id})


def _load_step(base_dir: Path, run_id: str, filename: str) -> dict[str, Any] | None:
    """读取某一步已保存的结果；还没做过则返回 None。只读本地文件。"""
    path = _run_path(base_dir, run_id) / filename
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        logger.exception(f"{filename} 读取失败", extra={"event": "step_load_failed", "run_id": run_id})
        raise RunNotFound(f"结果文件已损坏：{run_id}/{filename}（{e}）") from e


def save_insight(base_dir: Path, run_id: str, result: insight_mod.InsightResult) -> None:
    _save_step(base_dir, run_id, "insight.json", "insight", result.to_dict())


def load_insight(base_dir: Path, run_id: str) -> dict[str, Any] | None:
    return _load_step(base_dir, run_id, "insight.json")


def insight_and_save(
    run_id: str,
    cfg: AppConfig | None = None,
    *,
    base_dir: Path | None = None,
    llm: Any = None,
    sleep: Callable[[float], Any] = time.sleep,
) -> dict[str, Any]:
    """网页「提炼需求」：检查配置 → 读这次抓取的笔记和评论 → 调大模型 → 保存，返回结果。"""
    cfg = cfg or load_config()
    base = Path(base_dir) if base_dir else runs_dir(cfg)
    run = load_run(base, run_id)
    if llm is None:
        cfg.require("llm")
        llm = LlmClient(cfg, sleep=sleep, counter=CallCounter())
    result = insight_mod.extract(llm, run.notes, run.comments, cache_dir=cache_dir(cfg))
    save_insight(base, run_id, result)
    return result.to_dict()


# ---------------- ③新品概念 ----------------
def save_concepts(base_dir: Path, run_id: str, result: concept_mod.ConceptResult) -> None:
    _save_step(base_dir, run_id, "concepts.json", "concept", result.to_dict())


def load_concepts(base_dir: Path, run_id: str) -> dict[str, Any] | None:
    return _load_step(base_dir, run_id, "concepts.json")


def concept_and_save(
    run_id: str,
    cfg: AppConfig | None = None,
    *,
    base_dir: Path | None = None,
    llm: Any = None,
    sleep: Callable[[float], Any] = time.sleep,
) -> dict[str, Any]:
    """网页「生成概念」：读这次的需求洞察结果 → 调大模型 → 保存，返回结果。"""
    cfg = cfg or load_config()
    base = Path(base_dir) if base_dir else runs_dir(cfg)
    ins = load_insight(base, run_id)
    if not ins or not ins.get("needs"):
        raise concept_mod.ConceptInputError("这次抓取还没有提炼需求，请先完成「②需求洞察」")
    if llm is None:
        cfg.require("llm")
        llm = LlmClient(cfg, sleep=sleep, counter=CallCounter())
    result = concept_mod.generate(llm, ins, brands=cfg.concept.competitor_brands, cache_dir=cache_dir(cfg))
    save_concepts(base, run_id, result)
    return result.to_dict()
