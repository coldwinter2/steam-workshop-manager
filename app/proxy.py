#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
代理配置中心

管理 Steam 网页访问（商店 / 社区 / API）的出口代理。

- 配置来源：config.toml 的 [proxy] 段，可被同名环境变量覆盖（便于部署时灵活指定）。
- 代理类型：支持 socks5（默认），亦兼容 http / https。
- 本模块只负责「解析 / 合并配置」与「生成可读描述」；实际发起请求由
  steam_meta 通过 requests.Session.proxies 完成（server 启动时把
  resolve_config() 的结果注入 steam_meta.configure_proxy()）。
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
from urllib.parse import urlparse

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
