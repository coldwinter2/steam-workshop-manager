#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
服务端安装类型（模块化）

职责边界：**服务端只「区分」安装方式并决定如何下行内容，不执行安装**；
真正的安装动作由客户端 client/install/ 按同样的类型标记执行。

每个安装类型负责两件事：
  1. build_files：把某 Mod 的本地目录呈现为「客户端要下载的文件列表」
     （文件列表结构 = modid -> 文件列表；单文件 Mod 呈现为 modid + 扩展名）
  2. resolve：下载时把呈现名解析为真实可读的文件（rename 下行改名后的内容、
     extract 下行打包好的 zip）

内置类型：
  copy    原样分发，保留 <modid>/... 目录结构（默认）
  rename  下行时直接给出改名后的单文件（如 L4D2 appid 550 -> <modid>.vpk）
  extract 下行打包为 zip（如饥荒联机版），客户端下载后解压
  custom  通过 register() 注册自定义类型，两侧（服务端呈现 + 客户端安装）各自扩展

zip 缓存：<storage>/<appid>/.mod_zips/<modid>.zip，按源目录 mtime 失效。
"""

import zipfile
from pathlib import Path


class InstallType:
    """安装类型基类（服务端呈现 + 下行转换）。"""

    key = "copy"
    label = "原样复制"
    # True 表示把整个 Mod 呈现为单个文件（文件名 = modid + 扩展名）
    single_file = False

    def build_files(self, mod_dir: Path, modid: str, params: dict, cache_dir=None) -> list:
        """返回该 Mod 呈现给客户端的文件列表 [{name, size}]。"""
        raise NotImplementedError

    def resolve(self, mod_dir: Path, modid: str, params: dict,
                request_name: str, cache_dir=None):
        """下载解析：返回可流式读取的真实 Path，失败返回 None。"""
        raise NotImplementedError


# ------------------------- 工具 -------------------------
def _iter_files(mod_dir: Path) -> list:
    if not mod_dir.is_dir():
        return []
    return sorted((f for f in mod_dir.rglob("*") if f.is_file()),
                  key=lambda p: p.stat().st_size, reverse=True)


def _pick_main(mod_dir: Path):
    """选出 Mod 的「主文件」：优先最大文件（单文件 Mod 即其本体）。"""
    files = _iter_files(mod_dir)
    return files[0] if files else None


def ensure_zip(mod_dir: Path, cache_dir, modid: str):
    """把 mod_dir 内容打包为 zip（带缓存），返回 zip 路径；无内容返回 None。

    缓存失效：源目录中任一文件比缓存新则重建。
    """
    files = [f for f in (mod_dir.rglob("*") if mod_dir.is_dir() else []) if f.is_file()]
    if not files:
        return None
    if cache_dir is None:
        cache_dir = mod_dir.parent / ".mod_zips"
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache = cache_dir / f"{modid}.zip"

    newest = max(f.stat().st_mtime for f in files)
    if cache.exists() and cache.stat().st_mtime >= newest:
        return cache

    tmp = cache_dir / f"{modid}.zip.tmp"
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
            for f in files:
                zf.write(f, f.relative_to(mod_dir).as_posix())
        tmp.replace(cache)
    except Exception:  # noqa: BLE001
        if tmp.exists():
            tmp.unlink(missing_ok=True)
        return None
    return cache


# ------------------------- 内置类型 -------------------------
class CopyInstall(InstallType):
    """原样分发：文件名 = modid/相对路径（默认）。"""

    key = "copy"
    label = "原样复制"

    def build_files(self, mod_dir, modid, params, cache_dir=None):
        out = []
        for f in _iter_files(mod_dir)[::-1]:  # 恢复自然顺序
            rel = f.relative_to(mod_dir).as_posix()
            out.append({"name": f"{modid}/{rel}", "size": f.stat().st_size})
        return out

    def resolve(self, mod_dir, modid, params, request_name, cache_dir=None):
        # request_name 形如 modid/rel/path（含斜杠，由上层相对路径解析兜底）
        if "/" in request_name:
            rel = request_name.split("/", 1)[1]
            p = (mod_dir / rel).resolve()
            if p.is_file() and str(p).startswith(str(mod_dir.resolve())):
                return p
        return None


class RenameInstall(InstallType):
    """下行改名：呈现/下行 <modid><target_ext>（如 L4D2 -> .vpk）。"""

    key = "rename"
    label = "重命名"
    single_file = True

    @staticmethod
    def _ext(params: dict, src) -> str:
        ext = params.get("target_ext") or (src.suffix if src else "") or ".vpk"
        ext = str(ext)
        return ext if ext.startswith(".") else "." + ext

    def build_files(self, mod_dir, modid, params, cache_dir=None):
        src = _pick_main(mod_dir)
        if not src:
            return []
        ext = self._ext(params, src)
        return [{"name": f"{modid}{ext}", "size": src.stat().st_size}]

    def resolve(self, mod_dir, modid, params, request_name, cache_dir=None):
        # 内容 = 主文件，呈现名（下行改名后）= request_name
        return _pick_main(mod_dir)


class ExtractInstall(InstallType):
    """下行打包：呈现/下行 <modid>.zip，客户端下载后解压（如饥荒联机版）。"""

    key = "extract"
    label = "解压缩"
    single_file = True

    def build_files(self, mod_dir, modid, params, cache_dir=None):
        z = ensure_zip(mod_dir, cache_dir, modid)
        if not z:
            return []
        return [{"name": f"{modid}.zip", "size": z.stat().st_size}]

    def resolve(self, mod_dir, modid, params, request_name, cache_dir=None):
        return ensure_zip(mod_dir, cache_dir, modid)


# ------------------------- 注册表 -------------------------
REGISTRY: dict = {}


def register(itype: InstallType):
    """注册安装类型（便于扩展 custom 类型）。"""
    REGISTRY[itype.key] = itype
    return itype


def get_install_type(key: str) -> InstallType:
    """按 key 取安装类型；未知 key 回退到 copy。"""
    return REGISTRY.get(str(key), REGISTRY["copy"])


def available_types() -> list:
    """返回 [{"key","label","single_file"}]，供前端下拉选择。"""
    return [{"key": k, "label": v.label, "single_file": v.single_file}
            for k, v in REGISTRY.items()]


# 注册内置类型
register(CopyInstall())
register(RenameInstall())
register(ExtractInstall())
