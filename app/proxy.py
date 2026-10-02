#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
代理配置中心

管理 Steam 网页访问（商店 / 社区 / API）的出口代理。

- 配置来源：config.toml 的 [proxy] 段，可被同名环境变量覆盖（便于部署时灵活指定）。
- 代理类型：必须支持 socks5（默认），亦兼容 http / https。
- 生效范围：**仅 Python urllib 侧**（获取游戏名 / Mod 名 / 工坊依赖的网页与 API 请求），
  通过 PySocks 的 SocksiPyHandler 构造 opener，使所有请求经由 SOCKS5 代理
  （含远程 DNS 解析，避免域名被本地污染）。
- **不生效范围**：steamcmd 子进程（下载 Mod）不注入任何代理环境变量，
  保持其自身网络行为，避免代理链路影响下载稳定性。

环境变量（优先级高于 config.toml）：
  STEAM_PROXY_ENABLED : "1"/"true" 等表示启用
  STEAM_PROXY         : "host:port" 或 "socks5://host:port"
  STEAM_PROXY_HOST    : 代理主机
  STEAM_PROXY_PORT    : 代理端口
  STEAM_PROXY_TYPE    : socks5 / http / https
"""

import os
import urllib.request
from urllib.parse import urlparse

# PySocks 为可选依赖：缺少时 socks5 类型退化为直连，不阻断程序运行。
try:
    import socks
    from sockshandler import SocksiPyHandler
    _HAVE_SOCKS = True
except Exception:  # noqa: BLE001
    _HAVE_SOCKS = False


# 环境变量名
ENV_ENABLED = "STEAM_PROXY_ENABLED"
ENV_PROXY = "STEAM_PROXY"
ENV_HOST = "STEAM_PROXY_HOST"
ENV_PORT = "STEAM_PROXY_PORT"
ENV_TYPE = "STEAM_PROXY_TYPE"


def _default_config() -> dict:
    return {
        "enabled": False,
        "type": "socks5",
        "host": "127.0.0.1",
        "port": 1080,
        "username": "",
        "password": "",
    }


def resolve_config(toml_proxy: dict | None = None) -> dict:
    """合并 config.toml 的 [proxy] 与环境变量，返回最终生效的代理配置。

    toml_proxy 应为 config_manager.get_proxy() 返回的字典；
    环境变量优先级高于配置文件。
    """
    cfg = _default_config()
    if toml_proxy:
        for k in cfg:
            v = toml_proxy.get(k)
            if v not in (None, ""):
                cfg[k] = v

    # 环境变量覆盖
    env_enabled = os.environ.get(ENV_ENABLED)
    if env_enabled is not None:
        cfg["enabled"] = str(env_enabled).strip().lower() in (
            "1", "true", "yes", "y", "on"
        )

    env_proxy = os.environ.get(ENV_PROXY)
    if env_proxy:
        p = env_proxy.strip()
        if "://" in p:
            parsed = urlparse(p)
            if parsed.scheme:
                cfg["type"] = parsed.scheme.lower()
            cfg["host"] = parsed.hostname or cfg["host"]
            if parsed.port:
                cfg["port"] = parsed.port
        else:
            host, _, port = p.partition(":")
            cfg["host"] = host or cfg["host"]
            if port:
                try:
                    cfg["port"] = int(port)
                except ValueError:
                    pass

    env_host = os.environ.get(ENV_HOST)
    if env_host:
        cfg["host"] = env_host.strip()
    env_port = os.environ.get(ENV_PORT)
    if env_port:
        try:
            cfg["port"] = int(env_port)
        except ValueError:
            pass
    env_type = os.environ.get(ENV_TYPE)
    if env_type:
        cfg["type"] = env_type.strip().lower()

    return cfg


def describe(cfg: dict | None = None) -> str:
    """人类可读的代理描述，用于日志与错误提示。"""
    if cfg is None:
        cfg = resolve_config()
    if not cfg.get("enabled"):
        return "直连（未启用代理）"
    return f"{(cfg.get('type') or 'socks5').lower()}://{cfg.get('host')}:{cfg.get('port')}"


def build_opener(cfg: dict | None = None):
    """构造 urllib opener。

    - 启用 socks5 且已安装 PySocks：经 SOCKS5 代理（远程解析 DNS）。
    - 启用 http/https：经该 HTTP 代理。
    - 未启用：沿用系统默认行为（含系统环境变量代理）。

    关键：启用本功能代理时**必须显式传入 ProxyHandler**，否则 urllib 会补一个
    默认的 ProxyHandler（handler_order=100），它读取系统 http_proxy/https_proxy
    并**优先于** SocksiPyHandler（handler_order=500）生效，导致 SOCKS5 形同虚设。
    """
    if cfg is None:
        cfg = resolve_config()
    ptype = (cfg.get("type") or "socks5").lower()
    if not cfg.get("enabled"):
        return urllib.request.build_opener()

    if ptype in ("socks5", "socks5h"):
        if not _HAVE_SOCKS:
            # 缺少 PySocks：无法走 socks5，明确告警后直连（避免静默失败误导排查）
            print(f"[警告] 已启用 socks5 代理但未安装 PySocks，本次请求将直连。"
                  f"请执行: pip install PySocks sockshandler")
            return urllib.request.build_opener(urllib.request.ProxyHandler({}))
        host = cfg["host"]
        port = int(cfg["port"])
        username = cfg.get("username") or None
        password = cfg.get("password") or None
        handler = SocksiPyHandler(
            socks.PROXY_TYPE_SOCKS5, host, port, True, username, password
        )
        # ProxyHandler({}) 清空环境代理，保证 socks5 处理器真正生效
        return urllib.request.build_opener(urllib.request.ProxyHandler({}), handler)

    if ptype in ("http", "https"):
        url = f"{ptype}://{cfg['host']}:{int(cfg['port'])}"
        return urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": url, "https": url})
        )
    return urllib.request.build_opener()


def check(cfg: dict | None = None, target_host: str = "store.steampowered.com",
          target_port: int = 443, timeout: float = 10) -> tuple[bool, str]:
    """代理自检：验证「能否经代理连到目标主机」。

    覆盖连接建立环节：socks5 会完成 SOCKS5 握手 + 远程 DNS 解析 + 到目标的
    TCP 连接；http/https 仅验证代理端口可连（CONNECT 需真实请求才验证）。
    返回 (是否成功, 说明文本)。
    """
    import socket

    if cfg is None:
        cfg = resolve_config()
    desc = describe(cfg)
    if not cfg.get("enabled"):
        return _direct_check(target_host, target_port, timeout, desc)

    ptype = (cfg.get("type") or "socks5").lower()
    host, port = cfg["host"], int(cfg["port"])
    try:
        if ptype in ("socks5", "socks5h"):
            if not _HAVE_SOCKS:
                return False, f"{desc}：未安装 PySocks，无法建立 socks5 连接"
            sock = socks.create_connection(
                (target_host, target_port),
                proxy_type=socks.PROXY_TYPE_SOCKS5,
                proxy_addr=host, proxy_port=port, proxy_rdns=True,
                proxy_username=cfg.get("username") or None,
                proxy_password=cfg.get("password") or None,
                timeout=timeout,
            )
            peer = sock.getpeername()
            sock.close()
            return True, (f"{desc}：SOCKS5 握手成功，已连到 {target_host}:{target_port}"
                          f"（代理侧对端 {peer[0]}:{peer[1]}）")
        # http/https：仅验证代理端口可连
        with socket.create_connection((host, port), timeout=timeout):
            return True, f"{desc}：代理端口可连（CONNECT 需实际请求验证）"
    except Exception as e:  # noqa: BLE001
        return False, f"{desc}：连接失败 {type(e).__name__}: {e}"


def _direct_check(target_host: str, target_port: int, timeout: float,
                  desc: str) -> tuple[bool, str]:
    import socket

    try:
        with socket.create_connection((target_host, target_port), timeout=timeout):
            return True, f"{desc}：直连 {target_host}:{target_port} 成功"
    except Exception as e:  # noqa: BLE001
        return False, f"{desc}：直连失败 {type(e).__name__}: {e}"


def _self_check_cli():
    """命令行自检：python -m app.proxy"""
    cfg = resolve_config()
    print("生效代理:", describe(cfg))
    print("配置明细:", {k: cfg[k] for k in
                        ("enabled", "type", "host", "port")})
    for target in ("store.steampowered.com", "steamcommunity.com",
                   "api.steampowered.com"):
        ok, msg = check(cfg, target_host=target, target_port=443)
        print(("  [OK] " if ok else "  [!!] ") + msg)


if __name__ == "__main__":
    _self_check_cli()
