"""Step 2：配置加载（R1）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.config import ConfigError, load_config, mask_secret

FULL_ENV = {
    "XHS_BASE_URL": "https://xhs.example.com/",
    "XHS_API_KEY": "xhs-secret-123456",
    "LLM_BASE_URL": "https://llm.example.com/v1",
    "LLM_API_KEY": "llm-secret-abcdef",
    "LLM_MODEL": "kimi-test",
    "IMAGE_BASE_URL": "https://img.example.com/v1",
    "IMAGE_API_KEY": "img-secret-zzzzzz",
    "IMAGE_MODEL": "gpt-image-2",
}

YAML_OK = """
collect:
  max_notes: 10
  comments_per_note: 2
  max_comments: 20
  default_keywords: ["低温酸奶"]
http:
  max_retries: 3
  backoff_base_seconds: 1.0
xhs:
  timeout_seconds: 30
llm:
  timeout_seconds: 120
  temperature: 0.3
image:
  timeout_seconds: 180
  max_retries: 2
  size: "1024x1024"
paths:
  data_dir: data
  logs_dir: logs
"""


@pytest.fixture()
def yaml_file(tmp_path: Path) -> Path:
    p = tmp_path / "app.yaml"
    p.write_text(YAML_OK, encoding="utf-8")
    return p


def test_load_ok(yaml_file):
    cfg = load_config(env=FULL_ENV, yaml_path=yaml_file)
    assert cfg.xhs.api_key == "xhs-secret-123456"
    assert cfg.xhs.base_url == "https://xhs.example.com"  # 去掉结尾斜杠
    assert cfg.llm.model == "kimi-test"
    assert cfg.image.model == "gpt-image-2"
    cfg.require("xhs", "llm", "image")  # 全部齐全时不报错


def test_missing_required_names_field(yaml_file):
    env = dict(FULL_ENV)
    env["LLM_API_KEY"] = ""
    env.pop("IMAGE_BASE_URL")
    cfg = load_config(env=env, yaml_path=yaml_file)
    cfg.require("xhs")  # 聚道云齐全
    with pytest.raises(ConfigError) as ei:
        cfg.require("llm")
    assert "LLM_API_KEY" in str(ei.value)
    assert ei.value.missing == ["LLM_API_KEY"]
    with pytest.raises(ConfigError) as ei:
        cfg.require("llm", "image")
    assert set(ei.value.missing) == {"LLM_API_KEY", "IMAGE_BASE_URL"}
    assert cfg.missing("image") == ["IMAGE_BASE_URL"]


def test_missing_yaml_field_names_field(tmp_path):
    p = tmp_path / "app.yaml"
    p.write_text(YAML_OK.replace("  max_notes: 10\n", ""), encoding="utf-8")
    with pytest.raises(ConfigError) as ei:
        load_config(env=FULL_ENV, yaml_path=p)
    assert "collect.max_notes" in str(ei.value)


def test_missing_yaml_file(tmp_path):
    with pytest.raises(ConfigError) as ei:
        load_config(env=FULL_ENV, yaml_path=tmp_path / "nope.yaml")
    assert "nope.yaml" in str(ei.value)


def test_mask_secret_shows_first_4():
    assert mask_secret("abcdefghijkl") == "abcd****"
    assert mask_secret("abc") == "****"
    assert mask_secret("") == ""
    assert mask_secret(None) == ""


def test_config_repr_never_shows_secret(yaml_file):
    cfg = load_config(env=FULL_ENV, yaml_path=yaml_file)
    text = repr(cfg) + str(cfg) + str(cfg.safe_dict())
    for k in ("XHS_API_KEY", "LLM_API_KEY", "IMAGE_API_KEY"):
        assert FULL_ENV[k] not in text
    assert "xhs-****" in str(cfg.safe_dict())
    assert set(cfg.secrets()) == {FULL_ENV["XHS_API_KEY"], FULL_ENV["LLM_API_KEY"], FULL_ENV["IMAGE_API_KEY"]}


def test_yaml_values_loaded(yaml_file):
    cfg = load_config(env=FULL_ENV, yaml_path=yaml_file)
    assert cfg.collect.max_notes == 10
    assert cfg.collect.comments_per_note == 2
    assert cfg.collect.max_comments == 20
    assert cfg.collect.default_keywords == ["低温酸奶"]
    assert cfg.http.max_retries == 3
    assert cfg.xhs.timeout_seconds == 30
    assert cfg.llm.timeout_seconds == 120
    assert cfg.image.timeout_seconds == 180
    assert cfg.image.max_retries == 2
    assert cfg.image.size == "1024x1024"
    assert cfg.paths.logs_dir == Path("logs")


def test_repo_yaml_is_valid():
    """仓库自带的 config/app.yaml 必须能加载，且数量与需求一致。"""
    cfg = load_config(env=FULL_ENV)
    assert (cfg.collect.max_notes, cfg.collect.comments_per_note, cfg.collect.max_comments) == (10, 20, 200)
    assert cfg.image.timeout_seconds == 180


# ---- Step 5b：接口地址格式检查（修 BUG-002）----


def test_bug_image_base_url_docs_page(yaml_file):
    """BUG-002 现场：IMAGE_BASE_URL 填成了文档网页地址，应在配置检查时就报错并给出正确示例。"""
    env = dict(FULL_ENV, IMAGE_BASE_URL="https://bananarouter.com/docs/models")
    cfg = load_config(env=env, yaml_path=yaml_file)
    with pytest.raises(ConfigError) as ei:
        cfg.require("image")
    msg = str(ei.value)
    assert "IMAGE_BASE_URL" in msg and "/v1" in msg and "https://api.bananarouter.com/v1" in msg
    assert ei.value.missing == []  # 不是"没填"，是"填错"
    assert cfg.invalid("image") and not cfg.invalid("llm", "xhs")


def test_base_url_must_end_with_v1(yaml_file):
    env = dict(FULL_ENV, LLM_BASE_URL="https://api.kimi.com/coding")
    cfg = load_config(env=env, yaml_path=yaml_file)
    with pytest.raises(ConfigError, match="LLM_BASE_URL"):
        cfg.require("llm")


def test_base_url_must_be_http(yaml_file):
    env = dict(FULL_ENV, IMAGE_BASE_URL="api.bananarouter.com/v1", XHS_BASE_URL="ftp://xhs.example.com")
    cfg = load_config(env=env, yaml_path=yaml_file)
    with pytest.raises(ConfigError, match="IMAGE_BASE_URL"):
        cfg.require("image")
    with pytest.raises(ConfigError, match="XHS_BASE_URL"):
        cfg.require("xhs")


def test_valid_base_urls_pass(yaml_file):
    env = dict(
        FULL_ENV,
        XHS_BASE_URL="https://api.cn8n.com",  # 聚道云不要求 /v1
        LLM_BASE_URL="https://api.kimi.com/coding/v1/",  # 结尾斜杠会被去掉
        IMAGE_BASE_URL="https://api.bananarouter.com/v1",
    )
    cfg = load_config(env=env, yaml_path=yaml_file)
    assert cfg.invalid("xhs", "llm", "image") == []
    cfg.require("xhs", "llm", "image")


def test_empty_url_reported_as_missing_not_invalid(yaml_file):
    cfg = load_config(env=dict(FULL_ENV, IMAGE_BASE_URL=""), yaml_path=yaml_file)
    assert cfg.invalid("image") == []
    with pytest.raises(ConfigError) as ei:
        cfg.require("image")
    assert ei.value.missing == ["IMAGE_BASE_URL"]


def test_env_example_shows_full_image_url():
    text = (Path(__file__).resolve().parents[1] / ".env.example").read_text(encoding="utf-8")
    assert "https://api.bananarouter.com/v1" in text
