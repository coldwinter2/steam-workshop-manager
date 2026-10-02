#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""安装处理器注册表（便于扩展 custom 安装方式）。"""

from .base import InstallHandler

REGISTRY: dict = {}


def register(handler: InstallHandler):
    REGISTRY[handler.type_key] = handler
    return handler


def get_handler(type_key: str) -> InstallHandler:
    """按类型取处理器；未知类型回退到 copy（原样放置）。"""
    return REGISTRY.get(str(type_key), REGISTRY["copy"])


def available_keys() -> list:
    return list(REGISTRY.keys())


# 注册内置类型
from .copy import CopyHandler        # noqa: E402
from .extract import ExtractHandler  # noqa: E402
from .rename import RenameHandler    # noqa: E402

register(CopyHandler())
register(RenameHandler())
register(ExtractHandler())
