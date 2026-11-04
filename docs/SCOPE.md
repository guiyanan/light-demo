# 修改范围：光明乳业 · 小红书洞察驱动的新品概念生成 Demo

> 仓库：https://github.com/guiyanan/light-demo
> 状态：**草稿，待确认**（2026-11-04）
> 依据：`docs/REQUIREMENTS.md` v1.1
> 规则：开发中如需改动本文件未列出的内容，先更新 SCOPE.md 并写明理由，再动手。

---

## 1. 现状
- 仓库为空仓库（`main` 分支没有任何 commit），本期所有文件都是**新增**。
- 用户本地项目文件夹：`文稿/光明demo`（已连接到对话，目前为空），代码将 clone 到此处。

## 2. 允许新增的文件

| 路径 | 内容 | 对应需求 |
|---|---|---|
| `CLAUDE.md`、`AGENTS.md` | 全局开发流程摘要 + 本项目信息 | 流程 |
| `README.md` | 首次准备步骤、`start.command` 用法、接口检查方法 | S7 R17 |
| `docs/REQUIREMENTS.md` `SCOPE.md` `PLAN.md` `BUGS.md` | 需求、范围、方案、bug 记录 | 流程 |
| `app/__init__.py` `config.py` `logging_setup.py` | 配置、日志 | R1 R2 |
| `app/diag.py` | 接口检查（命令行 + 供网页调用） | R14 |
| `app/clients/http.py` | 公共 HTTP：重试、退避、计时、日志、打码、计数 | R2 R3 |
| `app/clients/xhs.py` | 聚道云：搜索笔记、获取评论、解析 | R4–R7 |
| `app/clients/llm.py` | Kimi（月之暗面开放平台）：JSON 输出、Schema 校验、重试、缓存 | R8a R9 |
| `app/clients/image.py` | BananaRouter `gpt-image-2`：b64_json / url | R10 |
| `app/pipeline/collect.py` | 抓取前 10 篇 + 每篇取 2 条评论 | R4–R7 |
| `app/pipeline/insight.py` | 提炼 5 条需求 + 原话子串校验 | R8a R8b |
| `app/pipeline/concept.py` | 生成 3 个概念 | R9 |
| `app/pipeline/render.py` | Pillow 合成展示图 | R11 R12 |
| `app/pipeline/export.py` `templates/report.html` | HTML 报告 + PNG zip | R16 |
| `app/pipeline/runs.py` | 运行记录保存与读取 | R15 |
| `app/pipeline/schemas.py` | 各步骤数据结构与 JSON Schema | R8a R9 |
| `app/ui/streamlit_app.py`、`app/ui/pages/*` | 接口检查页 + 4 步页面，只调用 pipeline | R13 R14 |
| `config/app.yaml` | 抓取数量、超时、重试、图片尺寸等 | R1 |
| `assets/logo/README.md` | 说明 logo 由客户提供 | R12 |
| `tests/**`、`tests/fixtures/**` | 单元测试与模拟数据 | 全部 |
| `start.command` | `git pull` + `docker compose up --build` | R17 |
| `Dockerfile`、`docker-compose.yml`、`.dockerignore` | 容器化、挂载 data/logs、中文字体 | R17 |
| `.env.example`、`.gitignore` | 配置样例与忽略规则 | R1 |
| `requirements.txt`、`requirements-dev.txt`、`pyproject.toml` | 依赖、ruff、pytest 配置 | 2.1 |
| `.github/workflows/ci.yml` | 代码检查 → 测试 → docker compose build → 上传测试日志 | S7 |

## 3. 只读 / 不得修改
| 内容 | 说明 |
|---|---|
| `docs/REQUIREMENTS.md` 的需求内容 | 变更须先经用户确认并升级版本号 |
| `.env` | 不提交、不写入日志；由用户本人填写密钥 |
| `data/runs/`、`data/cache/`、`logs/` | 运行产物，不提交 |
| `assets/logo/` 下的客户文件 | 只读取，不修改、不生成、不仿制 |
| 外部服务 | 只调用公开 API，不做爬虫、不使用小红书账号或 Cookie |

## 4. 明确不做
与需求文档第 5 节一致：不做热度公式、Excel、成本、可行性、合规、logo 生成、一键跑完、登录权限、生产级部署；不接其他数据源。

## 5. 风险点

| # | 风险 | 影响 | 应对 |
|---|---|---|---|
| 1 | 聚道云返回结构未知、可能没有广告字段 | 解析失败或无法标注广告 | 第 1 轮先做接口检查，按真实响应写解析器和 fixture；无广告字段时如实标为非广告并记日志 |
| 2 | BananaRouter 调用方式未知 | 生图失败 | 全部可配置；第 1 轮确认；失败时用灰色占位图 |
| 3 | Kimi Key 用成了 Kimi Code 会员 Key | 调用被拒 | 使用开放平台 Key；第 1 轮接口检查时验证 |
| 4 | 大模型编造原话、JSON 不合格 | 洞察不可信 | 原话必须是原文子串；Schema 校验 + 重试 1 次 |
| 5 | 生图把文字或假 logo 画到包装上 | 图文不一致 | 提示词禁止文字和 logo；文字全部由程序排版在右侧信息栏 |
| 6 | 开发环境连不上接口、没有 Docker | 无法在开发端真实联调 | 单元测试全部模拟；CI 做 docker build；真实联调在用户电脑，`logs/` 挂载到 `文稿/光明demo` 供直接读取 |
| 7 | 本会话对仓库只有读权限 | 无法推送 | 开工前需授予推送权限 |
| 8 | 空仓库无法直接走 PR | 首个 commit 只能进 `main` | Step 1 直接提交 `main`，之后每步走 `feature/*` → PR → CI 通过 → 合并 |
| 9 | 本地文件夹中 git 的删除操作默认被禁止 | clone 后 pull 失败 | `git pull` 由用户双击 `start.command` 在 Mac 上执行，不受此限制 |
| 10 | 接口费用 / 额度耗尽 | 演示中断 | 402 不重试并提示；大模型结果缓存；可直接展示历史运行结果 |
