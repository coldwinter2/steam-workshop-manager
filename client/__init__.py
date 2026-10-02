#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Mod 同步客户端

- 启动前在 client/config.py 中硬编码目标 AppID，仅拉取该游戏的 Mod
- 可配置本地安装路径保存下载的 Mod 文件
- 支持手动下载指定 Mod（及其依赖）、本地删除指定 Mod（及其孤儿依赖）
- 对服务端仅做只读 GET，任何客户端操作都不影响服务端文件

启动：python -m client
"""
