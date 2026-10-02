#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
客户端文案（i18n）

下载速度相关的展示文案集中在本文件，便于后续扩展其它语言。
当前仅内置 zh_CN，文案风格与既有界面保持一致（中文、全角括号、
同样的分隔符「 · 」与省略号「…」）。

既有界面文案仍内联在 ui.py 中（本次需求要求保持不变），
新增的下载速度文案统一走 t() 取词。
"""

FALLBACK = "zh_CN"
LANG = "zh_CN"

STRINGS = {
    "zh_CN": {
        # 下载方式
        "mode_normal": "常规下载",
        "mode_compressed": "压缩下载",
        # 速度状态（边界情况）
        "speed_waiting": "等待中…",
        "speed_stalled": "已暂停（无数据）",
        "speed_retry": "重试中（第 {n} 次）",
        "speed_done": "已完成",
        # 速度文案：rate 已带单位（如 12.3 MB/s）
        "speed_label": "速度 {rate}",
        "speed_effective": "等效 {rate}",
        "speed_network": "网络 {rate}",
        # 列表单元格（一行内并列展示两条速率）
        "speed_cell_compressed": "等效 {eff} / 网络 {wire}",
        # 详情行附加说明
        "speed_note_compressed": "（压缩传输）",
    },
}


def t(key: str, lang: str = None, **kwargs) -> str:
    """取词；支持 {name} 占位符。缺失时回落 zh_CN，再缺失则返回 key。"""
    table = STRINGS.get(lang or LANG) or STRINGS[FALLBACK]
    s = table.get(key)
    if s is None:
        s = STRINGS[FALLBACK].get(key, key)
    try:
        return s.format(**kwargs) if kwargs else s
    except Exception:  # noqa: BLE001 - 占位符不匹配时至少返回原文案
        return s


def set_lang(lang: str):
    """切换语言（未知语言自动回落 zh_CN）。"""
    global LANG
    LANG = lang if lang in STRINGS else FALLBACK
    return LANG
