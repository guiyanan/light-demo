# 光明 Demo · 开发约定（Claude / 其他 AI 助手必读）

## 项目
光明乳业 · 小红书洞察驱动的新品概念生成 Demo。需求、范围、方案见：
- `docs/REQUIREMENTS.md`（需求，变更须用户确认并升级版本号）
- `docs/SCOPE.md`（允许修改的文件；超出范围先改 SCOPE 并写理由）
- `docs/PLAN.md`（按 Step 实施，每步有勾选框和对应单元测试）
- `docs/BUGS.md`（bug 记录）

用户本地文件夹：`文稿/光明demo`（`~/Documents/光明demo`），根目录的 `项目进度.md` 是给新对话看的进度摘要（不提交）。

## 开发流程（全局规则）
1. 需求 → 根据需求写单元测试（先红）→ 写代码（变绿）
2. 每个 Step 写完先做一轮 Code Review
3. 全量测试，输出存 `logs/test/`
4. commit + push（`feature/*` 分支 → PR → GitHub Actions 通过 → 合并到 `main`）
5. 每轮结束后用户用真实接口在网页上测试，确认后才进入下一轮
6. bug 必须先写能稳定复现的测试，再修复，并记录到 `docs/BUGS.md`

## 代码约定
- 业务逻辑放在 `app/pipeline/`，外部接口放在 `app/clients/`，界面 `app/ui/` 只负责调用和展示
- 所有外部调用经过 `app/clients/http.py` 的重试/日志/打码逻辑
- 单元测试不依赖网络、随机数、当前时间和执行顺序；外部接口一律模拟
- 密钥只在 `.env`；日志与接口检查输出必须打码（只显示前 4 位）
- 不保存小红书用户身份信息（昵称、用户 ID、头像、IP 属地）

## 部署
`git pull` → 编辑 `.env` → `docker compose up --build`（`start.command` 已封装）。网页 http://localhost:8501
