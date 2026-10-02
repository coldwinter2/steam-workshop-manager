#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
安装处理器基类（客户端）

服务端只「区分」安装方式，客户端按同样的类型标记「执行」安装。
每个处理器把 staging 目录里下载好的文件落到最终安装位置，
返回产物路径（相对安装根目录），供状态记录与删除使用。

新增安装方式：写一个 InstallHandler 子类并 register()，
即可在服务端与客户端两侧各自扩展（服务端 app/install.py 负责呈现/下行）。
"""

from dataclasses import dataclass, field
from pathlib import Path


class InstallError(Exception):
    """安装过程错误。"""


@dataclass
class InstallContext:
    """一次安装的输入。"""

    staging_dir: Path      # 下载文件所在（staging 根）
    modid: str             # 当前 Mod ID
    target_root: Path      # 本地安装根目录（用户配置的 local_path）
    params: dict           # 安装参数（如 rename 的 target_ext）
    files: list = field(default_factory=list)  # 服务端呈现的文件名列表


@dataclass
class InstallResult:
    """安装结果。"""

    artifacts: list        # 相对 target_root 的产物路径（目录以 / 结尾）
    message: str = ""


class InstallHandler:
    """安装处理器基类。子类实现 install()。"""

    type_key = ""
    label = ""

    def install(self, ctx: InstallContext) -> InstallResult:
        raise NotImplementedError
