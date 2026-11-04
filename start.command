#!/bin/bash
# 光明 Demo 一键启动：在访达里双击本文件即可。
# 做三件事：拉取最新代码（git pull）→ 检查 .env → 构建并启动网页（docker compose up --build）

cd "$(dirname "$0")" || exit 1
export PATH="/usr/local/bin:/opt/homebrew/bin:/Applications/Docker.app/Contents/Resources/bin:$PATH"

mkdir -p logs/build data/runs data/cache
LOG="logs/build/start-$(date +%Y%m%d-%H%M%S).log"

say() { echo "$@" | tee -a "$LOG"; }
pause_exit() { echo; read -r -p "按回车键关闭窗口……" _; exit "${1:-1}"; }

say "===== 光明 Demo 启动 $(date) ====="

# 1. 拉取最新代码
say "[1/3] 拉取最新代码（git pull）……"
git pull --ff-only 2>&1 | tee -a "$LOG"
if [ "${PIPESTATUS[0]}" -ne 0 ]; then
  say "⚠️ 拉取代码失败（详见 $LOG），将使用本地已有代码继续启动。"
fi

# 2. 检查 .env
say "[2/3] 检查 .env……"
if [ ! -f .env ]; then
  cp .env.example .env
  say "已生成 .env，请填写密钥后保存，再重新双击 start.command。"
  open -e .env
  pause_exit 1
fi

# 3. 构建并启动
say "[3/3] 构建并启动网页（docker compose up --build）……"
if ! command -v docker >/dev/null 2>&1; then
  say "❌ 没有找到 Docker，请先安装并打开 Docker Desktop。"
  pause_exit 1
fi
if ! docker info >/dev/null 2>&1; then
  say "Docker Desktop 没有运行，正在打开，请稍等……"
  open -a Docker
  for _ in $(seq 1 60); do
    sleep 2
    docker info >/dev/null 2>&1 && break
  done
  if ! docker info >/dev/null 2>&1; then
    say "❌ Docker Desktop 2 分钟内没有启动完成，请手动打开后再双击本文件。"
    pause_exit 1
  fi
fi

# 网页启动需要一点时间，稍后自动打开浏览器
( for _ in $(seq 1 90); do
    sleep 2
    if curl -s -o /dev/null http://localhost:8501/_stcore/health; then
      open http://localhost:8501; exit 0
    fi
  done ) &

# Mac 开着代理时，让 Docker 构建也走代理（否则下载中文字体会失败，见 docs/BUGS.md BUG-001）
PROXY="$(scutil --proxy 2>/dev/null | bash scripts/docker_proxy.sh)"
if [ -n "$PROXY" ]; then
  say "检测到系统代理，构建时使用：$PROXY"
  docker compose build --build-arg HTTP_PROXY="$PROXY" --build-arg HTTPS_PROXY="$PROXY" 2>&1 | tee -a "$LOG"
else
  docker compose build 2>&1 | tee -a "$LOG"
fi
if [ "${PIPESTATUS[0]}" -ne 0 ]; then
  say "❌ 构建失败，详见 $LOG"
  pause_exit 1
fi

say "网页地址：http://localhost:8501 （关闭本窗口或按 Ctrl+C 即可停止）"
docker compose up 2>&1 | tee -a "$LOG"
say "===== 已停止 $(date) ====="
pause_exit 0
