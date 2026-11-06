"""Step 17（流程部分）：3 张图同时生成、保存、失败用占位图、单张重画（R10、R11、R15）。生图一律模拟。"""

from __future__ import annotations

import copy
import datetime as dt
import io
import json
import threading
import time

import pytest
from PIL import Image

from app.clients.http import QuotaError, ServerError
from app.clients.image import GeneratedImage
from app.config import load_config
from app.pipeline import render, runs
from tests.test_concept import INSIGHT, THREE, gen
from tests.test_insight import _collect_result


def png_bytes(color=(90, 160, 220)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (64, 64), color).save(buf, "PNG")
    return buf.getvalue()


class FakeImage:
    """按提示词决定成功或失败；记录调用和同时进行的最大请求数。"""

    def __init__(self, fail: dict[str, Exception] | None = None, delay: float = 0.0):
        self.fail = fail or {}
        self.prompts: list[str] = []
        self.delay = delay
        self.active = 0
        self.max_active = 0
        self.lock = threading.Lock()

    def generate(self, prompt, size=None):
        with self.lock:
            self.prompts.append(prompt)
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            time.sleep(self.delay)
            for key, err in self.fail.items():
                if key in prompt:
                    raise err
            return GeneratedImage(png_bytes(), "png", "b64_json", 10)
        finally:
            with self.lock:
                self.active -= 1


@pytest.fixture()
def run(tmp_path):
    run_id = runs.save_collect(_collect_result(), tmp_path, suffix="img1", now=dt.datetime(2026, 11, 5, 12, 0))
    (tmp_path / run_id / "insight.json").write_text(json.dumps(INSIGHT, ensure_ascii=False), encoding="utf-8")
    data = copy.deepcopy(THREE)  # 不改动共享样本
    for c in data["concepts"]:  # 每个概念用不同的生图描述，便于区分
        c["image_prompt"] = f"{c['name']} 的包装"
    res, _ = gen(data)
    runs.save_concepts(tmp_path, run_id, res)
    return tmp_path, run_id


def concept_prompt(i):
    return THREE["concepts"][i]["name"]


def test_three_generated_in_parallel_and_saved(run):
    base, run_id = run
    img = FakeImage(delay=0.05)
    out = runs.images_and_save(run_id, load_config(env={}), base_dir=base, image_client=img)
    assert img.max_active == 3  # 3 张同时画
    assert len(img.prompts) == 3 and all("logo" in p for p in img.prompts)  # 提示词带不要文字和 logo 的约束
    assert [it["no"] for it in out["items"]] == [1, 2, 3]
    for it in out["items"]:
        assert it["ok"] is True and it["placeholder"] is False
        card = base / run_id / it["card"]
        assert card.is_file() and Image.open(card).size == (1600, 1000)
        assert (base / run_id / it["raw"]).is_file()
    assert out["stats"] == {"images": 3, "failed": 0, "image_calls": 3}
    stats = json.loads((base / run_id / "stats.json").read_text(encoding="utf-8"))
    assert "image" in stats["steps"]


def test_failure_uses_placeholder_others_ok(run, read_logs):
    base, run_id = run
    err = ServerError("HTTP 503：busy", service="image", status_code=503, request_id="rid-img-2")
    img = FakeImage(fail={concept_prompt(1): err})
    out = runs.images_and_save(run_id, load_config(env={}), base_dir=base, image_client=img)
    bad = out["items"][1]
    assert bad["ok"] is False and bad["placeholder"] is True and bad["raw"] is None
    assert "服务端" in bad["error"] and bad["request_id"] == "rid-img-2"
    card = Image.open(base / run_id / bad["card"])
    assert card.getpixel((500, 900)) == render.PLACEHOLDER_GRAY  # 占位图，信息栏照常
    assert out["items"][0]["ok"] and out["items"][2]["ok"]
    assert out["stats"]["failed"] == 1
    assert any(r.get("event") == "image_failed" for r in read_logs("app"))


def test_only_regenerates_selected(run):
    base, run_id = run
    quota = QuotaError("HTTP 402", service="image", status_code=402)
    runs.images_and_save(run_id, load_config(env={}), base_dir=base, image_client=FakeImage(fail={concept_prompt(2): quota}))
    first = runs.load_images(base, run_id)
    assert first["items"][2]["ok"] is False and "额度" in first["items"][2]["error"]
    img = FakeImage()
    out = runs.images_and_save(run_id, load_config(env={}), base_dir=base, image_client=img, only=[3])
    assert len(img.prompts) == 1 and concept_prompt(2) in img.prompts[0]  # 只重画第 3 张
    assert out["items"][2]["ok"] is True and out["stats"]["failed"] == 0
    assert out["items"][0] == first["items"][0]  # 其他两张保持不变
    assert out["stats"]["image_calls"] == 4  # 累计调用次数


def test_load_images_no_api_calls(run, monkeypatch):
    base, run_id = run
    assert runs.load_images(base, run_id) is None
    runs.images_and_save(run_id, load_config(env={}), base_dir=base, image_client=FakeImage())

    def boom(*a, **k):  # 读取历史结果时不允许创建生图客户端
        raise AssertionError("不应调用接口")

    monkeypatch.setattr(runs, "ImageClient", boom)
    saved = runs.load_images(base, run_id)
    assert len(saved["items"]) == 3
    assert runs.card_path(base, run_id, saved["items"][0]).is_file()
    # 还没生成概念 → 提示先做第③步
    (base / run_id / "concepts.json").unlink()
    with pytest.raises(runs.StepInputError):
        runs.images_and_save(run_id, load_config(env={}), base_dir=base, image_client=FakeImage())
