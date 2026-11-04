"""Step 5a：Docker 构建自动走 Mac 系统代理（R17，修 BUG-001）。

scripts/docker_proxy.sh 从标准输入读取 `scutil --proxy` 的输出，
打印 Docker 构建可用的代理地址；没有开启代理时什么都不打印。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "docker_proxy.sh"

# 用户 Mac 上 `scutil --proxy` 的真实输出（BUG-001 现场）
SCUTIL_LOCAL_PROXY = """<dictionary> {
  ExceptionsList : <array> {
    0 : 127.0.0.1
    1 : localhost
  }
  FTPPassive : 1
  HTTPEnable : 1
  HTTPPort : 10808
  HTTPProxy : 127.0.0.1
  HTTPSEnable : 1
  HTTPSPort : 10808
  HTTPSProxy : 127.0.0.1
  ProxyAutoConfigEnable : 0
  SOCKSEnable : 1
  SOCKSPort : 10808
  SOCKSProxy : 127.0.0.1
}
"""

SCUTIL_NO_PROXY = """<dictionary> {
  ExceptionsList : <array> {
    0 : *.local
  }
  FTPPassive : 1
  HTTPEnable : 0
  HTTPSEnable : 0
}
"""

SCUTIL_REMOTE_PROXY = """<dictionary> {
  HTTPEnable : 1
  HTTPPort : 8080
  HTTPProxy : proxy.corp.example.com
  HTTPSEnable : 1
  HTTPSPort : 8080
  HTTPSProxy : proxy.corp.example.com
}
"""


def run_script(stdin: str) -> str:
    result = subprocess.run(
        ["bash", str(SCRIPT)], input=stdin, capture_output=True, text=True, timeout=10, check=True
    )
    return result.stdout.strip()


def test_bug_docker_build_ignores_mac_proxy():
    # 本机代理 127.0.0.1 在容器里要换成 host.docker.internal 才能访问
    assert run_script(SCUTIL_LOCAL_PROXY) == "http://host.docker.internal:10808"


def test_proxy_disabled_returns_empty():
    assert run_script(SCUTIL_NO_PROXY) == ""


def test_remote_proxy_host_kept():
    assert run_script(SCUTIL_REMOTE_PROXY) == "http://proxy.corp.example.com:8080"


def test_empty_input_returns_empty():
    assert run_script("") == ""


def test_start_command_passes_proxy_build_args():
    text = (ROOT / "start.command").read_text(encoding="utf-8")
    assert "scripts/docker_proxy.sh" in text
    assert "--build-arg HTTP_PROXY=" in text
    assert "--build-arg HTTPS_PROXY=" in text
