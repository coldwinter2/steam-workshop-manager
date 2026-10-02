#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ANSI 转义序列处理。

问题背景
--------
steamcmd 等 CLI 工具在部分平台（如 Ubuntu）会向 stdout 写入 ANSI 转义序列以
呈现颜色/加粗，例如 ``ESC[0m``（重置）、``ESC[1m``（加粗）。这些序列在**终端**里
表现为颜色，但一旦被管道捕获、写入日志文件或 systemd journal，就会变成
``[0m``、``[1m`` 之类的可见乱码，破坏阅读与后续解析。

本模块提供
----------
- ``strip_ansi(text)``：剔除文本中的全部 ANSI 转义序列，只保留纯文本内容
  （保留原有文字、级别、时间戳等字段）。
- ``use_color(mode, stream)``：根据配置（``auto`` / ``always`` / ``never``）与
  输出目标是否为终端（TTY）决定是否允许颜色输出，作为「仅 TTY 才上色 /
  可配置关闭」的统一入口。
- ``colored(text, *names, mode, stream)``：在上色被允许时包裹颜色序列，否则
  原样返回。
"""

import re
import sys

# 覆盖以下几类（steamcmd / 常见 CLI 输出的全部形态）：
#   1) CSI 序列：ESC [ <参数> <中间> <终结>  如 ESC[0m ESC[1m ESC[32;1m
#   2) OSC 序列：ESC ] ... BEL(0x07) 或 ESC \ (ST)
#   3) 字符集选择等单参数转义：ESC ( B 等
#   4) 8-bit CSI 引入符 0x9B 及其序列
#   5) 任何孤立的 ESC(0x1b)/0x9B，避免残留可打印乱码
_ANSI_RE = re.compile(
    r"\x1b\[[\x30-\x3f]*[\x20-\x2f]*[\x40-\x7e]"      # CSI
    r"|\x1b\][^\x07\x1b]*?(\x07|\x1b\\)"               # OSC -> BEL/ST
    r"|\x1b[\(\)][AB0-2]"                              # 字符集选择
    r"|\x9b[\x30-\x3f]*[\x20-\x2f]*[\x40-\x7e]"        # 8-bit CSI
    r"|\x1b|\x9b"                                       # 孤立引入符
)


def strip_ansi(text: str) -> str:
    """剔除文本中的全部 ANSI 转义序列，保留纯文本。

    空字符串或不含转义的文本原样返回（零拷贝），保证性能与正确性。
    """
    if not text:
        return text
    if "\x1b" not in text and "\x9b" not in text:
        return text
    return _ANSI_RE.sub("", text)


# 基础调色板（仅作为「允许的终端」下的点缀，不影响日志解析）
_COLORS = {
    "reset": "\x1b[0m",
    "bold": "\x1b[1m",
    "red": "\x1b[31m",
    "green": "\x1b[32m",
    "yellow": "\x1b[33m",
    "cyan": "\x1b[36m",
    "gray": "\x1b[90m",
}


def use_color(mode: str = "auto", stream=None) -> bool:
    """是否允许颜色输出。

    auto   —— 仅当目标流是终端（TTY）时上色，管道/journal/文件均不上色（默认、最安全）
    always —— 始终上色（仅在你确定输出到彩色终端时使用）
    never  —— 永远不上色（日志文件/journal/管道场景用这个彻底关闭）
    """
    mode = (mode or "auto").lower()
    if mode == "never":
        return False
    if mode == "always":
        return True
    stream = stream or sys.stderr
    try:
        return bool(stream.isatty())
    except Exception:  # noqa: BLE001 部分流没有 isatty
        return False


def colored(text: str, *names: str, mode: str = "auto", stream=None) -> str:
    """在上色被允许时给文本加颜色/样式；不允许时原样返回。"""
    if not use_color(mode, stream):
        return text
    prefix = "".join(_COLORS.get(n, "") for n in names)
    if not prefix:
        return text
    return prefix + text + _COLORS["reset"]
