# 方案文档：光明乳业 · 小红书洞察驱动的新品概念生成 Demo

> 状态：**草稿，待确认**（2026-11-04）
> 依据：`docs/REQUIREMENTS.md` v1.3、`docs/SCOPE.md`
> 每个 Step 的规则：先写测试（红）→ 写代码（绿）→ 全量测试（输出存 `logs/test/`）→ Code Review → commit/push → CI 绿灯，全部满足才能勾选。方案变更先改本文件。
> 每**轮**结束后：用户在访达里双击 `start.command`，在网页上用真实接口测试；**用户确认没问题后才进入下一轮**。发现问题按 bug 流程处理：先写复现测试 → 修复 → 记入 `docs/BUGS.md`。
> 所有外部接口在单元测试中一律模拟；测试不依赖网络、随机数、当前时间和执行顺序。

## 总览

| 轮次 | Step | 本轮结束后你在网页上测什么 |
|---|---|---|
| 1 基础 + 接口检查 | 1–5 | 接口检查页：3 个接口是否连通 |
| 2 数据抓取 | 6–9 | 输入关键词 → 10 篇笔记 + 20 条评论，广告有标注；可选历史结果 |
| 3 需求洞察 | 10–12 | 5 条需求 + 原话证据 |
| 4 新品概念 | 13–14 | 3 个概念卡片 |
| 5 产品图 | 15–17 | 3 张合成图 |
| 6 导出 | 18–19 | 下载报告和图片 |

---

## 第 1 轮：基础 + 接口检查

- [x] **Step 1：项目骨架 + CI + Docker + start.command**
  - 首次准备（Claude 执行）：把仓库 clone 到 `文稿/光明demo`，从 `.env.example` 生成 `.env`（密钥留空）；用户只需打开 `.env` 粘贴密钥
  - 对应需求：R17、S7
  - 修改文件：`CLAUDE.md` `AGENTS.md` `README.md` `docs/*.md` `.gitignore` `.env.example` `requirements*.txt` `pyproject.toml` `app/__init__.py` `app/ui/streamlit_app.py`（空白首页） `Dockerfile` `docker-compose.yml` `.dockerignore` `start.command` `.github/workflows/ci.yml`
  - 单元测试：`tests/test_skeleton.py::test_app_importable`、`::test_gitignore_excludes_secrets_and_runtime`（`.env`、`logs/`、`data/runs/`、`data/cache/`）、`::test_env_example_has_all_keys`、`::test_start_command_runs_pull_and_compose`、`::test_compose_mounts_data_and_logs`
  - 说明：首个 commit 直接进 `main`，之后开启分支保护，后续走 `feature/*` → PR
  - Commit：2baadba

- [x] **Step 2：配置加载**
  - 对应需求：R1
  - 修改文件：`app/config.py` `config/app.yaml`
  - 单元测试：`tests/test_config.py::test_load_ok`、`::test_missing_required_names_field`、`::test_mask_secret_shows_first_4`、`::test_yaml_values_loaded`
  - Commit：765a671

- [x] **Step 3：结构化日志**
  - 对应需求：R2
  - 修改文件：`app/logging_setup.py`
  - 单元测试：`tests/test_logging.py::test_record_has_required_fields`、`::test_error_goes_to_error_dir_with_stack`、`::test_secret_never_in_log`、`::test_request_id_propagates`、`::test_step_logger_records_start_end_count_elapsed`
  - Commit：d97b43d

- [x] **Step 4：HTTP 公共层**
  - 对应需求：R2、R3
  - 修改文件：`app/clients/http.py`
  - 单元测试：`tests/test_http.py::test_400_401_402_no_retry`、`::test_429_5xx_backoff_max_retries`、`::test_timeout_raises_typed_error`、`::test_log_has_method_url_status_elapsed_without_key`、`::test_call_counter`
  - 说明：退避的等待函数可注入，测试中不真实等待
  - Commit：5a06776

- [x] **Step 5：接口检查（命令行 + 网页"接口检查"页）**
  - 对应需求：R14、R13（部分）
  - 修改文件：`app/diag.py` `app/clients/xhs.py`（最小搜索调用） `app/clients/llm.py`（最小调用） `app/clients/image.py`（最小调用） `app/ui/pages/0_接口检查.py`
  - 单元测试：`tests/test_diag.py::test_all_success`、`::test_partial_failure_reports_status_and_elapsed`、`::test_raw_saved_masked`、`::test_no_secret_in_output`；`tests/test_ui_diag.py::test_page_renders_three_checks`（Streamlit AppTest，diag 模拟）
  - Commit：7a27531

- [x] **Step 5a：start.command 自动让 Docker 构建走 Mac 代理（修 BUG-001）**
  - 对应需求：R17
  - 修改文件：`scripts/docker_proxy.sh`（新增） `start.command`
  - 单元测试：`tests/test_docker_proxy.py::test_bug_docker_build_ignores_mac_proxy`、`::test_proxy_disabled_returns_empty`、`::test_remote_proxy_host_kept`、`::test_empty_input_returns_empty`、`::test_start_command_passes_proxy_build_args`
  - Commit：2930c97

- [x] **Step 5b：接口地址格式检查（修 BUG-002）**
  - 对应需求：R1、R14
  - 修改文件：`app/config.py` `app/diag.py`（接口检查时也做地址检查，直接提示） `.env.example`
  - 单元测试：`tests/test_config.py::test_bug_image_base_url_docs_page`、`::test_base_url_must_end_with_v1`、`::test_base_url_must_be_http`、`::test_valid_base_urls_pass`、`::test_empty_url_reported_as_missing_not_invalid`、`::test_env_example_shows_full_image_url`；`tests/test_diag.py::test_bad_base_url_fails_without_calling`
  - Commit：3b06b43

**🔍 第 1 轮测试（你来）**：打开"接口检查"页，依次点 3 个按钮，看是否都显示成功。
**我随后做**：读取 `文稿/光明demo/logs/diag/` 中的脱敏响应，确认聚道云数据结构（含是否有广告字段）、生图调用方式，据此生成第 2、5 轮的测试数据。

---

## 第 2 轮：数据抓取

- [x] **Step 6：搜索笔记解析 + 广告标注 + 隐私过滤**
  - 对应需求：R4、R5、R7
  - 修改文件：`app/clients/xhs.py` `tests/fixtures/xhs/search_*.json`（基于第 1 轮真实响应脱敏）
  - 单元测试：`tests/test_xhs_search.py::test_parse_real_structure`、`::test_keep_api_order_and_rank`、`::test_stop_at_10`、`::test_stop_when_no_more`、`::test_multi_keyword_round_robin_dedupe`、`::test_keyword_count_1_to_3`、`::test_ad_flag`、`::test_no_ad_field_defaults_false_and_logs`、`::test_no_user_identity_fields`
  - 说明：广告标记来自笔记 widgets_context 的 order_cooperate / commercial_bridge_note（商业合作笔记）；原始响应保存前去除身份信息
  - Commit：a272614

- [x] **Step 7：评论抓取 + 每篇取点赞最高 2 条**
  - 对应需求：R6、R7
  - 修改文件：`app/clients/xhs.py` `app/pipeline/collect.py` `tests/fixtures/xhs/comments_*.json`
  - 单元测试：`tests/test_collect.py::test_top2_by_likes_per_note`、`::test_fewer_than_2_comments`、`::test_total_at_most_20`、`::test_one_page_per_note`、`::test_comment_fields_no_identity`
  - 说明：评论接口参数 sort_strategy=like_count（实测确认）；单篇失败跳过并记录，401/402 直接停止
  - Commit：7b730cb

- [x] **Step 8：运行记录保存与读取**
  - 对应需求：R15
  - 修改文件：`app/pipeline/runs.py`
  - 单元测试：`tests/test_runs.py::test_save_layout`（raw/、notes.jsonl、comments.jsonl、stats.json）、`::test_load_roundtrip`、`::test_list_runs_newest_first`、`::test_load_makes_zero_api_calls`
  - Commit：d8e0c42

- [x] **Step 9：网页"①数据抓取"页**
  - 对应需求：R13、R5、R15
  - 修改文件：`app/ui/pages/1_数据抓取.py` `app/pipeline/runs.py`（新增 collect_and_save：检查配置 → 抓取 → 保存，供网页一次调用） `app/ui/streamlit_app.py`（首页菜单说明）
  - 单元测试：`tests/test_ui_collect.py::test_input_keywords_and_run`、`::test_ad_label_shown`、`::test_select_history_run`、`::test_error_shows_summary_request_id_and_retry`（pipeline 全部模拟）；`tests/test_runs.py::test_collect_and_save`、`::test_collect_and_save_requires_xhs_config`
  - Commit：faae8bb

- [x] **Step 9a：按点赞排序返回空页时改用默认排序（修 BUG-003）**
  - 对应需求：R6
  - 修改文件：`app/clients/xhs.py` `app/pipeline/collect.py`
  - 单元测试：`tests/test_collect.py::test_bug_like_count_empty_page_falls_back_to_default`、`::test_empty_page_without_comments_no_fallback`
  - Commit：d0dfd3b

- [x] **Step 9b：候选 40 篇，按评论数挑 10 篇**（需求 v1.2，用户 2026-11-05 确认）
  - 对应需求：R4
  - 修改文件：`config/app.yaml`（candidate_notes、min_note_comments） `app/config.py` `app/pipeline/collect.py` `app/ui/pages/1_数据抓取.py`（说明文字）
  - 单元测试：`tests/test_collect.py::test_pick_notes_by_comment_count`、`::test_pick_notes_min_comments_threshold`、`::test_pick_notes_tie_keeps_search_order`、`::test_search_40_candidates`
  - Commit：7c35df6

- [x] **Step 9c：每篇保留整页评论 + 过滤水评论**（需求 v1.2，用户 2026-11-05 确认）
  - 对应需求：R6
  - 修改文件：`config/app.yaml` `app/pipeline/collect.py` `app/clients/xhs.py`
  - 单元测试：`tests/test_collect.py::test_is_junk_comment`、`::test_keep_whole_page_sorted_by_likes`、`::test_junk_filtered_and_counted`
  - Commit：4aa06fe

**🔍 第 2 轮测试（你来）**：输入"低温酸奶"点抓取，检查是否有 10 篇讨论较多的笔记、评论是否比之前更有内容（水评论已去掉）、广告是否标注；再从历史记录里选刚才那次，确认能直接打开。

---

## 第 3 轮：需求洞察

- [x] **Step 10：大模型客户端（JSON 输出、校验、重试、缓存）**
  - 对应需求：R8a（基础）
  - 修改文件：`app/clients/llm.py` `app/pipeline/schemas.py` `requirements.txt`（加 jsonschema）
  - 单元测试：`tests/test_llm.py::test_json_schema_ok`、`::test_invalid_json_retry_once_then_error`、`::test_cache_by_content_hash`、`::test_base_url_model_configurable`、`::test_json_in_code_fence_parsed`
  - Commit：686e83a

- [x] **Step 11：提炼 5 条需求 + 原话真实性校验**
  - 对应需求：R8a、R8b
  - 修改文件：`app/pipeline/insight.py` `app/pipeline/schemas.py` `app/pipeline/runs.py`（保存 / 读取 insight.json） `config/app.yaml`（llm 超时 180 秒） `tests/fixtures/insight/*`
  - 输入：每篇标题 + 正文开头 + 全部保留评论；广告帖参与并标记（需求 v1.3）
  - 单元测试：`tests/test_insight.py::test_five_needs_schema_valid`、`::test_quote_source_note_or_comment`、`::test_non_substring_quote_dropped_and_counted`、`::test_need_without_quotes_retry_then_flag`、`::test_fewer_than_5_returns_actual`、`::test_ads_included_and_marked`、`::test_insight_saved_and_loaded`
  - Commit：5480248

- [x] **Step 12：网页"②需求洞察"页**
  - 对应需求：R13
  - 修改文件：`app/ui/pages/2_需求洞察.py` `app/ui/streamlit_app.py`（首页使用说明）
  - 单元测试：`tests/test_ui_insight.py::test_five_needs_with_quotes_rendered`、`::test_error_and_retry`、`::test_ad_quote_labeled`
  - Commit：347b945

- [x] **Step 12a：聚道云故障时给出明确提示，修 BUG-004**
  - 对应需求：R13（失败显示原因）
  - 问题：聚道云把自己上游的 402 包装成 502「下游系统调用失败」，页面只显示「接口服务端出错」，用户看不出原因
  - 修改文件：`app/clients/xhs.py` `docs/BUGS.md`
  - 单元测试：`tests/test_xhs_search.py::test_bug_juhe_upstream_failure_hint`
  - Commit：78a5be0

**🔍 第 3 轮测试（你来）**：点"提炼需求"，看 5 条需求是否合理，原话能否在笔记或评论里找到。

---

## 第 4 轮：新品概念

- [ ] **Step 13：生成 3 个新品概念**
  - 对应需求：R9
  - 修改文件：`app/pipeline/concept.py` `app/pipeline/schemas.py`
  - 单元测试：`tests/test_concept.py::test_three_concepts_schema_valid`、`::test_need_ids_within_1_to_5`、`::test_nutrition_marked_ai_estimate`、`::test_image_prompt_present`
  - Commit：

- [ ] **Step 14：网页"③新品概念"页**
  - 对应需求：R13
  - 修改文件：`app/ui/pages/3_新品概念.py`
  - 单元测试：`tests/test_ui_concept.py::test_three_cards_all_fields`、`::test_error_and_retry`
  - Commit：

**🔍 第 4 轮测试（你来）**：点"生成概念"，看 3 个概念的字段是否齐全、内容是否和需求对得上。

---

## 第 5 轮：产品图

- [ ] **Step 15：生图客户端**
  - 对应需求：R10、R12
  - 修改文件：`app/clients/image.py` `tests/fixtures/image/*`（基于第 1 轮真实响应）
  - 单元测试：`tests/test_image.py::test_b64_json`、`::test_url_download`、`::test_timeout`、`::test_400_no_retry`、`::test_5xx_retry_max_2`、`::test_prompt_forbids_text_and_logo`
  - Commit：

- [ ] **Step 16：合成展示图**
  - 对应需求：R11、R12
  - 修改文件：`app/pipeline/render.py` `assets/logo/README.md`
  - 单元测试：`tests/test_render.py::test_canvas_1600x1000`、`::test_rendered_text_equals_concept_data`、`::test_cjk_font_available`、`::test_watermark_present`、`::test_logo_slot_reserved_empty`、`::test_failed_image_uses_gray_placeholder`
  - Commit：

- [ ] **Step 17：网页"④产品图"页**
  - 对应需求：R13
  - 修改文件：`app/ui/pages/4_产品图与导出.py`
  - 单元测试：`tests/test_ui_image.py::test_three_images_shown`、`::test_single_image_retry`
  - Commit：

**🔍 第 5 轮测试（你来）**：点"生成图片"，检查 3 张图：包装上没有乱字，右侧文字和概念一致，失败时显示灰色占位图和重试按钮。

---

## 第 6 轮：导出

- [ ] **Step 18：导出 HTML 报告 + PNG zip**
  - 对应需求：R16
  - 修改文件：`app/pipeline/export.py` `app/pipeline/templates/report.html`
  - 单元测试：`tests/test_export.py::test_report_contains_needs_quotes_concepts`、`::test_images_embedded`、`::test_ad_label_in_report`、`::test_zip_contains_3_png`、`::test_no_user_identity`
  - Commit：

- [ ] **Step 19：导出按钮 + 端到端测试 + README 收尾**
  - 对应需求：S1–S7
  - 修改文件：`app/ui/pages/4_产品图与导出.py` `tests/test_e2e.py` `README.md`
  - 单元测试：`tests/test_e2e.py::test_full_flow_mocked`（模拟 3 个接口：抓取 → 5 条需求 → 3 个概念 → 3 张图 → 导出）；`tests/test_ui_export.py::test_download_buttons`
  - Commit：

**🔍 第 6 轮测试（你来）**：下载报告，用浏览器打开并打印成 PDF，检查内容是否完整；下载图片压缩包。

---

## 开工前需要你确认 / 提供
1. 确认本 PLAN、SCOPE 和 REQUIREMENTS v1.1（确认后才开始 Step 1）
2. 给本会话仓库推送权限（目前只读）
3. Kimi 模型 ID、BananaRouter Base URL（可以等到第 1 轮测试前再提供）
