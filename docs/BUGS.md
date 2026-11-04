# Bug 记录

> 规则：先写能稳定复现的测试 → 再修复 → 记录在这里。

| 编号 | 日期 | 现象 | 复现测试 | 原因 | 修复 commit |
|---|---|---|---|---|---|
| BUG-001 | 2026-11-04 | 用户 Mac 上 `docker compose up --build` 失败：`E: Failed to fetch http://deb.debian.org/.../fonts-noto-cjk_20240730+repack1-1_all.deb  Connection failed [IP: 199.232.162.132 80]`，apt exit code 100；连续 3 次均失败（日志 `logs/build/deploy_*.log`） | `tests/test_docker_proxy.py::test_bug_docker_build_ignores_mac_proxy` | Mac 系统代理开着（127.0.0.1:10808），Mac 本机下载正常（约 500KB/s）；Docker 构建不使用系统代理，容器内直连下载速度为 0。经代理（host.docker.internal:10808）容器内下载约 400KB/s | 新增 `scripts/docker_proxy.sh` 读取 `scutil --proxy`，`start.command` 构建时传 `HTTP_PROXY/HTTPS_PROXY` build-arg：2930c97 |
| BUG-002 | 2026-11-04 | 接口检查「生图」失败：`[image HTTP 200 invalid_response] 返回内容不是 JSON`，返回的是 HTML 网页；请求 URL `https://bananarouter.com/docs/models/images/generations`（日志 `logs/diag/*image-generate-fd5325e0d02f.json`） | `tests/test_config.py::test_bug_image_base_url_docs_page` | `.env` 的 IMAGE_BASE_URL 填成了文档网页地址；配置加载没有检查地址格式，错误要到调用时才暴露且提示不明确 | `require()` 检查接口地址须为 http(s) 且以 `/v1` 结尾，报错给出正确示例；`.env.example` 写明完整地址：3b06b43 |
