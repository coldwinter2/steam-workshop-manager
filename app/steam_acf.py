#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Steam ACF（Valve KeyValues）读写与「删除 Mod 后同步维护下载记录」

## 为什么需要这个模块

steamcmd 判断「某个创意工坊 Mod 是否已下载」**不看文件是否存在**，而是看
`<install_dir>/steamapps/workshop/appworkshop_<appid>.acf` 里是否有该 itemid 的记录：

    "AppWorkshop"
    {
        "appid"        "550"
        "SizeOnDisk"   "3454306439"
        ...
        "WorkshopItemsInstalled"
        {
            "213445426"
            {
                "size"          "203791"
                "timeupdated"   "1790454103"
                "manifest"      "-1"
                "ugchandle"     "-4673088071463086127"
            }
            ...
        }
        "WorkshopItemDetails" { ... 同上，另含 timetouched / latest_* ... }
    }

只删除 Mod 的文件目录、而不摘掉这两处记录时，steamcmd 会认为该 Mod **已安装**
并直接跳过 `workshop_download_item`，于是「删掉再重新下载」永远拿不回文件
（日志通常只显示 `Success. Downloaded item ...` 却没有实际落盘内容）。
因此删除 Mod 必须连带维护 ACF。

## 格式要点（按真实文件实测）

- 纯 ASCII/UTF-8 无 BOM，行尾 **LF**（无 CR），文件以 LF 结尾
- 缩进用 TAB；键与值之间固定 **2 个 TAB**（不做列对齐）
- 字符串一律带双引号，数字/负数同样以字符串存储（如 `"-1"`）

本模块的解析/序列化**保序**（`OrderedDict`），只改写需要改写的部分，
未涉及的键值原样保留；`SizeOnDisk` 等字段不做重算（其口径含磁盘块对齐，
与各 Mod `size` 之和并不相等）。
"""

import os
import re
from collections import OrderedDict
from pathlib import Path

__all__ = [
    "AcfError",
    "parse",
    "dump",
    "acf_candidates",
    "find_acf",
    "remove_items",
    "remove_acf_file",
]

# ACF 里记录工坊条目的两个子块（删除时要同时摘掉）
INSTALLED_BLOCK = "WorkshopItemsInstalled"
DETAILS_BLOCK = "WorkshopItemDetails"

# 合法的 appid/itemid：纯数字（直接参与文件名/键名拼接，防注入）
_ID_RE = re.compile(r"^\d+$")

# 反斜杠转义表（Valve KeyValues 支持，实际少见但需正确回读）
_UNESCAPE = {"n": "\n", "t": "\t", "r": "\r", "\\": "\\", '"': '"'}


class AcfError(Exception):
    """ACF 文件解析/写入错误。"""


# ------------------------- 解析 -------------------------
class _Parser:
    """极简 KeyValues 解析器（字符串值 + 嵌套块）。"""

    def __init__(self, text: str):
        self.text = text
        self.i = 0
        self.n = len(text)

    def _skip_ws(self):
        t, n = self.text, self.n
        i = self.i
        while i < n and t[i] in " \t\r\n":
            i += 1
        self.i = i

    def _token(self):
        """读一个 token：'{'、'}' 或字符串内容；EOF 返回 None。"""
        self._skip_ws()
        if self.i >= self.n:
            return None
        c = self.text[self.i]
        if c == "{":
            self.i += 1
            return "{"
        if c == "}":
            self.i += 1
            return "}"
        if c == '"':
            return self._quoted()
        raise AcfError(f"位置 {self.i} 处出现意外字符 {c!r}")

    def _quoted(self):
        t, n = self.text, self.n
        i = self.i + 1          # 跳过起始引号
        buf: list[str] = []
        while i < n and t[i] != '"':
            if t[i] == "\\" and i + 1 < n:
                i += 1
                buf.append(_UNESCAPE.get(t[i], t[i]))
            else:
                buf.append(t[i])
            i += 1
        if i >= n:
            raise AcfError("存在未闭合的双引号")
        self.i = i + 1          # 跳过结束引号
        return "".join(buf)

    def block(self, depth: int = 0) -> OrderedDict:
        out: OrderedDict = OrderedDict()
        while True:
            tok = self._token()
            if tok is None:
                if depth > 0:
                    raise AcfError("块未闭合（缺少 '}'）")
                return out
            if tok == "}":
                if depth == 0:
                    raise AcfError("多余的 '}'")
                return out
            if tok == "{":
                raise AcfError("出现了没有键名的 '{'")
            key = tok
            val = self._token()
            if val is None:
                raise AcfError(f"键 {key!r} 缺少取值")
            if val == "{":
                out[key] = self.block(depth + 1)
            elif val == "}":
                raise AcfError(f"键 {key!r} 缺少取值")
            else:
                out[key] = val


def parse(text: str) -> tuple[str, OrderedDict]:
    """解析 ACF 文本，返回 (根键名, 根块)。根键名通常是 "AppWorkshop"。"""
    p = _Parser(text)
    root_key = p._token()
    if not root_key or root_key in ("{", "}"):
        raise AcfError("文件为空或缺少根键名")
    if p._token() != "{":
        raise AcfError(f"根键 {root_key!r} 后缺少 '{{'")
    data = p.block(depth=1)
    # 文件尾允许存在空白（真实文件以 LF 结尾）
    if p._token() is not None:
        raise AcfError("根块结束后仍有多余内容")
    return root_key, data


# ------------------------- 序列化 -------------------------
def _dump_body(lines: list, data: dict, level: int):
    pad = "\t" * level
    for key, val in data.items():
        if isinstance(val, dict):
            lines.append(f'{pad}"{key}"')
            lines.append(f"{pad}{{")
            _dump_body(lines, val, level + 1)
            lines.append(f"{pad}}}")
        else:
            # 键与值之间固定 2 个 TAB（与 steamcmd 输出一致）
            lines.append(f'{pad}"{key}"\t\t"{val}"')


def dump(root_key: str, data: dict) -> str:
    """按 steamcmd 的格式序列化（TAB 缩进、键值间 2 个 TAB、LF 行尾）。"""
    lines = [f'"{root_key}"', "{"]
    _dump_body(lines, data, 1)
    lines.append("}")
    return "\n".join(lines) + "\n"


# ------------------------- 定位 -------------------------
def acf_candidates(appid: str, install_dir=None, extra_dirs=None) -> list[Path]:
    """列出该 appid 的 ACF 可能所在路径（按优先级排序）。

    steamcmd 用 `+force_install_dir X` 时，ACF 落在 `X/steamapps/workshop/`；
    未指定时落在 **steamcmd 自身目录**的 `steamapps/workshop/`。两种都查。
    """
    appid = str(appid)
    if not _ID_RE.match(appid):
        raise AcfError(f"非法 appid: {appid!r}")
    name = f"appworkshop_{appid}.acf"
    roots: list[Path] = []
    for d in [install_dir, *(extra_dirs or [])]:
        if not d:
            continue
        try:
            p = Path(d)
        except (TypeError, ValueError):
            continue
        roots.append(p)
    out: list[Path] = []
    for r in roots:
        out.append(r / "steamapps" / "workshop" / name)   # 标准位置
    return out


def find_acf(appid: str, install_dir=None, extra_dirs=None) -> Path | None:
    """返回实际存在的 ACF 路径；都不存在则返回 None。"""
    for p in acf_candidates(appid, install_dir, extra_dirs):
        if p.is_file():
            return p
    return None


# ------------------------- 维护 -------------------------
def _strip_item(block, itemid: str) -> bool:
    """从子块中删除该 itemid，返回是否确实删掉了。"""
    if not isinstance(block, dict):
        return False
    for key in (itemid, str(itemid)):
        if key in block:
            del block[key]
            return True
    return False


def remove_items(
    appid: str,
    itemids,
    install_dir=None,
    extra_dirs=None,
    steamcmd_root=None,
) -> dict:
    """从该 appid 的 ACF 中摘掉若干 Mod 的下载记录（删除 Mod 时调用）。

    只改 `WorkshopItemsInstalled` / `WorkshopItemDetails` 两个子块，
    其余字段（含 `SizeOnDisk`）原样保留；写入采用「临时文件 + 原子替换」，
    避免中途失败留下半个文件。

    找不到 ACF 时不算错误（可能从未下载过，或目录已被整体删除），
    此时所有 itemid 归入 `missing`。

    返回::

        {"path": str | None,        # 实际改写的 ACF 路径
         "removed": [itemid...],    # 成功摘除记录的
         "missing": [itemid...],    # ACF 中本来就没有的
         "error": str | None}       # 出错信息（不改写文件）
    """
    appid = str(appid)
    if not _ID_RE.match(appid):
        return {"path": None, "removed": [], "missing": [str(i) for i in itemids],
                "error": f"非法 appid: {appid!r}"}

    wanted = []
    seen = set()
    for raw in itemids:
        s = str(raw)
        if not _ID_RE.match(s):
            return {"path": None, "removed": [], "missing": [s],
                    "error": f"非法 itemid: {s!r}"}
        if s not in seen:
            seen.add(s)
            wanted.append(s)
    if not wanted:
        return {"path": None, "removed": [], "missing": [], "error": None}

    extra = list(extra_dirs or [])
    if steamcmd_root:
        extra.append(steamcmd_root)
    path = find_acf(appid, install_dir, extra)
    if path is None:
        return {"path": None, "removed": [], "missing": wanted, "error": None}

    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return {"path": str(path), "removed": [], "missing": wanted,
                "error": f"{type(e).__name__}: {e}"}

    try:
        root_key, data = parse(text)
    except AcfError as e:
        return {"path": str(path), "removed": [], "missing": wanted,
                "error": f"ACF 解析失败: {e}"}

    removed, missing = [], []
    for itemid in wanted:
        hit = _strip_item(data.get(INSTALLED_BLOCK), itemid)
        hit |= _strip_item(data.get(DETAILS_BLOCK), itemid)
        (removed if hit else missing).append(itemid)

    if not removed:
        # 记录本就不存在：无需改写文件（保留原文件字节不变）
        return {"path": str(path), "removed": [], "missing": missing, "error": None}

    try:
        new_text = dump(root_key, data)
    except Exception as e:  # noqa: BLE001
        return {"path": str(path), "removed": [], "missing": wanted,
                "error": f"ACF 序列化失败: {type(e).__name__}: {e}"}

    try:
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(new_text, encoding="utf-8", newline="\n")
        os.replace(tmp, path)          # 原子替换（POSIX/Windows 均支持）
    except OSError as e:
        try:
            path.with_name(path.name + ".tmp").unlink(missing_ok=True)
        except OSError:
            pass
        return {"path": str(path), "removed": [], "missing": wanted,
                "error": f"ACF 写入失败: {type(e).__name__}: {e}"}

    return {"path": str(path), "removed": removed, "missing": missing, "error": None}


def remove_acf_file(appid: str, install_dir=None, extra_dirs=None,
                    steamcmd_root=None) -> dict:
    """删除整个 `appworkshop_<appid>.acf`（删除某个游戏时调用）。

    游戏整体删除后该 ACF 已无意义；留着会让 steamcmd 认为工坊条目仍在。
    文件不存在不算错误。

    返回 {"path": 相对提示用路径或 None, "deleted": bool, "error": str | None}
    """
    appid = str(appid)
    if not _ID_RE.match(appid):
        return {"path": None, "deleted": False, "error": f"非法 appid: {appid!r}"}
    extra = list(extra_dirs or [])
    if steamcmd_root:
        extra.append(steamcmd_root)
    path = find_acf(appid, install_dir, extra)
    if path is None:
        return {"path": None, "deleted": False, "error": None}
    try:
        path.unlink()
    except OSError as e:
        return {"path": str(path), "deleted": False,
                "error": f"{type(e).__name__}: {e}"}
    return {"path": str(path), "deleted": True, "error": None}
