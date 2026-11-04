# 光明乳业 · 小红书洞察驱动的新品概念生成 Demo

从小红书笔记和评论中提炼消费者需求，生成新品概念和包装效果图的演示项目。

## 首次准备（只做一次）
1. 安装并打开 **Docker Desktop**。
2. 打开项目文件夹（文稿/光明demo）里的 `.env` 文件（访达中按 `Cmd+Shift+.` 可显示隐藏文件），
   把三个接口的地址和密钥粘贴进去，保存。各项含义见文件里的中文注释。

## 每次启动
在访达里双击 **`start.command`**。它会：
1. 拉取最新代码（`git pull`）
2. 构建并启动网页（`docker compose up --build`）
3. 自动打开浏览器 http://localhost:8501

第一次构建要下载依赖，可能需要几分钟。要停止时关闭终端窗口即可。

> 如果双击后提示"无法打开"，在 `start.command` 上点右键 → 打开，确认一次即可。

## 接口检查
网页左侧菜单 →「接口检查」，分别点击三个按钮测试聚道云、Kimi、生图接口。
也可以在命令行运行：`docker compose run --rm web python -m app.diag`

## 日志和运行结果在哪里
- `logs/app/`：运行日志；`logs/error/`：错误日志（含出错位置）
- `logs/diag/`：接口检查的原始响应（已脱敏）
- `logs/build/`：每次启动的构建输出
- `data/runs/`：每次运行的结果

## 开发者
```bash
pip install -r requirements-dev.txt
ruff check . && pytest
```
文档：`docs/REQUIREMENTS.md`、`docs/SCOPE.md`、`docs/PLAN.md`、`docs/BUGS.md`。
