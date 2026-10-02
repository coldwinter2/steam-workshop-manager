#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
文件分发（复用 demo 的服务端思路）

为客户端提供：
  - 文件清单（demo 兼容的扁平 /files）
  - 分组清单 build_manifest：modid -> {元数据, 安装方式, 文件列表}
    （文件列表结构 = modid 映射该 Mod 的文件；单文件 Mod 呈现为 modid+扩展名）
  - 下载解析 resolve_download：区分安装方式，rename 下行改名后的文件、
    extract 下行打包好的 zip、copy 原样；旧相对路径仍兼容 demo

服务端只「区分」安装方式并决定下行内容，安装动作由客户端执行。
game 在分发语义中等同于 appid。
"""

import urllib.parse
from pathlib import Path

from .config_manager import ConfigManager
from .install import get_install_type
from .sync_manager import SyncManager


def list_projects(config: ConfigManager) -> list[dict]:
    """返回可分发项目（游戏）列表。"""
    projects = []
    for g in config.get_games():
        projects.append({
            "name": g["appid"],
            "description": g.get("name", ""),
            "target_path": str(SyncManager(config).content_dir_for(g["appid"])),
        })
    return projects


def files_for_game(sync: SyncManager, appid: str) -> dict | None:
    """返回某游戏下所有已下载文件清单（扁平，兼容 demo 客户端）。"""
    content_dir = sync.content_dir_for(appid)
    if not content_dir.exists():
        return None
    files = []
    for f in content_dir.rglob("*"):
        if f.is_file():
            files.append({
                "name": str(f.relative_to(content_dir)).replace("\\", "/"),
                "size": f.stat().st_size,
            })
    return {
        "project": appid,
        "native_path": str(content_dir),
        "target_path": str(content_dir),
        "files": files,
        "count": len(files),
    }


def _zip_cache_dir(sync: SyncManager, appid: str) -> Path:
    return sync.install_dir_for(appid) / ".mod_zips"


def build_manifest(config: ConfigManager, sync: SyncManager, appid: str) -> dict | None:
    """分组清单：modid -> {元数据 + 安装方式 + 文件列表}。

    文件列表按 mod 的安装方式呈现（install.py）：
      copy    -> ["<modid>/rel", ...]
      rename  -> ["<modid>.vpk"]   （单文件，modid+扩展名）
      extract -> ["<modid>.zip"]   （单文件，客户端解压）
    available=False 表示服务端尚未下载该 Mod（无可分发文件）。
    """
    appid = str(appid)
    game = next((g for g in config.get_games() if g["appid"] == appid), None)
    if game is None:
        return None

    content_dir = sync.content_dir_for(appid)
    cache_dir = _zip_cache_dir(sync, appid)
    mods_out: dict = {}
    for m in game["mods"]:
        modid = m["itemid"]
        install = m.get("install") or {"type": "copy", "params": {}}
        itype = get_install_type(install.get("type", "copy"))
        mod_dir = content_dir / modid
        files = itype.build_files(mod_dir, modid, install.get("params") or {}, cache_dir)
        mods_out[modid] = {
            "itemid": modid,
            "name": m.get("name", ""),
            "source": m.get("source", "manual"),
            "deps": m.get("deps", []),
            "install": {"type": itype.key, "params": install.get("params") or {}},
            "files": files,
            "available": bool(files),
        }
    return {
        "appid": appid,
        "name": game.get("name", ""),
        "install_types": _available_types(),
        "mods": mods_out,
        "count": len(mods_out),
    }


def _available_types() -> list:
    from .install import available_types
    return available_types()


def _safe_resolve(base: Path, rel: str) -> Path | None:
    """相对 base 解析文件并防路径穿越。"""
    try:
        target = (base / rel).resolve()
    except Exception:  # noqa: BLE001
        return None
    if not str(target).startswith(str(base.resolve())):
        return None
    if not target.is_file():
        return None
    return target


def resolve_download(config: ConfigManager, sync: SyncManager,
                     appid: str, request_path: str):
    """下载解析：区分安装方式。

    返回 (真实可读 Path, 呈现名) 或 None。
      - 含 "/" 的请求：旧相对路径（copy / demo），相对 content 目录解析
      - 单文件呈现名 modid[.ext]：按该 Mod 的安装方式转换内容
        （rename -> 主文件、extract -> 缓存 zip）
    """
    appid = str(appid)
    request_path = urllib.parse.unquote(request_path or "").replace("\\", "/").lstrip("/")
    if not request_path:
        return None
    content_dir = sync.content_dir_for(appid)

    if "/" in request_path:
        p = _safe_resolve(content_dir, request_path)
        return (p, Path(request_path).name) if p else None

    # 单文件呈现名：解析 modid（前导数字）+ 扩展名
    modid = request_path.split(".", 1)[0]
    if not modid.isdigit():
        p = _safe_resolve(content_dir, request_path)
        return (p, Path(request_path).name) if p else None

    install = config.get_mod_install(appid, modid)
    itype = get_install_type(install.get("type", "copy"))
    mod_dir = content_dir / modid
    cache_dir = _zip_cache_dir(sync, appid)
    real = itype.resolve(mod_dir, modid, install.get("params") or {},
                         request_path, cache_dir)
    if real is not None and real.is_file():
        return real, request_path
    return None


def resolve_file(sync: SyncManager, appid: str, rel_path: str) -> Path | None:
    """根据相对路径解析真实文件，并防止路径穿越（旧接口，兼容保留）。"""
    return _safe_resolve(sync.content_dir_for(appid), rel_path)
