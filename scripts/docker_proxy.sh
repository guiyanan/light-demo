#!/bin/bash
# 从标准输入读取 macOS `scutil --proxy` 的输出，打印 Docker 构建可用的代理地址。
# 没有开启 HTTP/HTTPS 代理时什么都不打印。
# 本机代理（127.0.0.1 / localhost）在容器里访问不到，换成 host.docker.internal。
# 用法：scutil --proxy | bash scripts/docker_proxy.sh

awk '
  { gsub(/^[ \t]+|[ \t]+$/, "") }
  $1 == "HTTPEnable"  { http_on  = $3 }
  $1 == "HTTPProxy"   { http_host = $3 }
  $1 == "HTTPPort"    { http_port = $3 }
  $1 == "HTTPSEnable" { https_on  = $3 }
  $1 == "HTTPSProxy"  { https_host = $3 }
  $1 == "HTTPSPort"   { https_port = $3 }
  END {
    if (http_on == "1" && http_host != "" && http_port != "") { host = http_host; port = http_port }
    else if (https_on == "1" && https_host != "" && https_port != "") { host = https_host; port = https_port }
    else exit 0
    if (host == "127.0.0.1" || host == "localhost" || host == "::1") host = "host.docker.internal"
    print "http://" host ":" port
  }
'
