#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一键把 tdlas-mcp 通过 cloudflared 隧道暴露为远端可直链的 MCP URL（安全默认）。

设计要点（隐私 / 安全）：
  · MCP HTTP 服务器仅监听 127.0.0.1 —— 隧道在本地连接，外部无法直连你本机端口（最小暴露面）
  · 强制 Bearer token 鉴权：取自环境变量 TDLAS_TOKEN，否则随机生成；不落库、不进仓库
  · cloudflared 隧道把 http://127.0.0.1:PORT 映射为公网 https URL
    （不出防火墙端口、Cloudflare 替你挡源 IP、流量加密），比路由器端口映射隐私得多
  · 隧道经代理出网时自动用 HTTPS_PROXY / TDLAS_TUNNEL_PROXY，并切到 http2 协议以兼容 HTTP 代理

用法：
  HTTPS_PROXY=http://127.0.0.1:7892 python tools/remote_link.py
  或： TDLAS_TOKEN=你的密钥 TDLAS_PORT=8000 python tools/remote_link.py
远端客户端填输出的 MCP URL + Bearer 即可直链。
"""
import os
import re
import sys
import time
import signal
import secrets
import subprocess
from shutil import which

PORT = int(os.environ.get("TDLAS_PORT", "8000"))
TOKEN = os.environ.get("TDLAS_TOKEN") or secrets.token_hex(16)
HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(HERE, "tdlas_mcp.py")
PY = sys.executable
PROXY = (os.environ.get("TDLAS_TUNNEL_PROXY")
         or os.environ.get("HTTPS_PROXY")
         or os.environ.get("HTTP_PROXY") or "")


def find_cloudflared():
    for name in ("cloudflared", "cloudflared.exe"):
        if which(name):
            return name
    local = os.path.join(HERE, "cloudflared.exe")
    return local if os.path.exists(local) else None


def main():
    # 1) 本地起 MCP HTTP 服务器（仅 127.0.0.1 + token）
    srv = subprocess.Popen(
        [PY, SERVER, "--http", "--host", "127.0.0.1", "--port", str(PORT), "--token", TOKEN],
        stderr=subprocess.PIPE, text=True)
    time.sleep(1.5)
    if srv.poll() is not None:
        print("[remote_link] MCP 服务器启动失败：", srv.stderr.read(), file=sys.stderr)
        sys.exit(1)
    print(f"[remote_link] MCP 服务器已起：127.0.0.1:{PORT}（仅本地）；token 前缀 {TOKEN[:8]}…")

    # 2) 找 cloudflared
    cf = find_cloudflared()
    if not cf or not os.path.exists(cf):
        print("[remote_link] 未找到 cloudflared，请先安装：", file=sys.stderr)
        print("  Windows: winget install Cloudflare.cloudflared", file=sys.stderr)
        print("  或下载 https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe 放到 tools/ 下", file=sys.stderr)
        srv.terminate()
        sys.exit(2)

    # 3) 起隧道
    env = dict(os.environ)
    cmd = [cf, "tunnel", "--url", f"http://127.0.0.1:{PORT}"]
    if PROXY:
        env["HTTPS_PROXY"] = PROXY
        env["HTTP_PROXY"] = PROXY
        cmd += ["--protocol", "http2"]  # HTTP 代理只能代理 TCP，强制 http2
    print(f"[remote_link] 启动 cloudflared 隧道（代理={PROXY or '直连'}）…")
    tunnel = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              text=True, env=env)

    url = None
    try:
        for line in tunnel.stdout:
            sys.stdout.write(line)
            sys.stdout.flush()
            m = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", line)
            if m and not url:
                url = m.group(0)
                break
    except Exception:
        pass

    if not url:
        print("[remote_link] 未从 cloudflared 输出解析到 URL（代理/网络问题？）", file=sys.stderr)
        srv.terminate()
        tunnel.terminate()
        sys.exit(3)

    print("\n" + "=" * 64)
    print("远端直链已就绪")
    print(f"  MCP URL : {url}/mcp")
    print(f"  Bearer  : {TOKEN}")
    print("  客户端配置：")
    print('  { "mcpServers": { "tdlas": { "type": "http",')
    print(f'    "url": "{url}/mcp",')
    print(f'    "headers": {{ "Authorization": "Bearer {TOKEN}" }} }} }}')
    print("=" * 64)
    print("按 Ctrl+C 停止（会同时关闭本地服务器与隧道）。")

    try:
        tunnel.wait()
    except KeyboardInterrupt:
        pass
    finally:
        srv.terminate()
        tunnel.terminate()


if __name__ == "__main__":
    main()
