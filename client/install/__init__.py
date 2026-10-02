#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
客户端安装模块（模块化）

服务端只标记安装方式（app/install.py 决定下行内容），
客户端按同一类型标记执行安装（client/install/ 决定落地位置）。

内置：copy / rename / extract；自定义安装方式通过 register() 注册。
"""

from .base import InstallContext, InstallError, InstallHandler, InstallResult
from .registry import available_keys, get_handler, register

__all__ = [
    "InstallContext",
    "InstallHandler",
    "InstallResult",
    "InstallError",
    "register",
    "get_handler",
    "available_keys",
]
