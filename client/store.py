#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
本地存储与安装管线（客户端）

流程：**下载到 staging -> 按安装方式分发安装 -> 记录状态**。

- 只读写「本地安装路径」，绝不操作服务端文件
- 下载：把服务端呈现的文件（modid->文件列表）落到 <local_path>/.staging/
- 安装：按 Mod 的 install.type 分发到 client/install/ 处理器，
  落到最终位置（copy -> <modid>/、rename -> <modid>.ext、extract -> <modid>/）
- 状态：<local_path>/.installed.json 记录每个 Mod 的类型与产物路径，
  用于状态检测与删除（支持离线删除、自定义安装方式）
"""

import json
import shutil
import time
from pathlib import Path
from typing import Callable, Optional

from . import api
from .install import InstallContext, get_handler


class StoreError(Exception):
    """本地存储/安装错误。"""


def _state_path(local_path: str) -> Path:
    return Path(local_path) / ".installed.json"


def load_state(local_path: str) -> dict:
    p = _state_path(local_path)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            return {}
    return {}


def save_state(local_path: str, state: dict):
    p = _state_path(local_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def _artifacts_exist(root: Path, artifacts: list) -> bool:
    if not artifacts:
        return False
    for a in artifacts:
        p = root / a
        if str(a).endswith("/"):
            if not (p.is_dir() and any(p.iterdir())):
                return False
        elif not p.exists():
            return False
    return True


def installed_mods(local_path: str) -> set:
    """已安装的 Mod 集合（状态记录 + 产物存在）。"""
    state = load_state(local_path)
    root = Path(local_path)
    out = set()
    for modid, info in state.items():
        if _artifacts_exist(root, info.get("artifacts", [])):
            out.add(str(modid))
    return out


def mod_size(local_path: str, itemid: str) -> int:
    """某个 Mod 本地占用字节数（按状态产物求和）。"""
    info = load_state(local_path).get(str(itemid), {})
    root = Path(local_path)
    total = 0
    for a in info.get("artifacts", []):
        p = root / a
        if str(a).endswith("/") and p.is_dir():
            for f in p.rglob("*"):
                if f.is_file():
                    total += f.stat().st_size
        elif p.is_file():
            total += p.stat().st_size
    return total


def download_and_install(
    server_url: str,
    local_path: str,
    appid: str,
    mod_meta: dict,
    progress_cb: Optional[Callable[[str, int, int], None]] = None,
    compress: bool = False,
    traffic_cb: Optional[Callable[[int], None]] = None,
) -> dict:
    """下载某 Mod 的呈现文件到 staging，并按安装方式执行安装。

    compress=False（默认，常规下载）：原始文件直传，服务端不压缩；
    compress=True（压缩下载）：请求服务端 gzip 压缩传输。

    traffic_cb(wire_bytes) 可选：上报**整个 Mod 累计**的网络接收字节
    （progress_cb 的 done 是解压后的原始字节）。压缩下载时两者不同，
    供界面同时展示「网络速率」与「等效速率」；常规下载两者相等。

    mod_meta 来自分组清单：{itemid, name, install:{type,params}, files:[{name,size}]}
    返回 {bytes, artifacts, message, type}；失败抛 StoreError。
    """
    modid = str(mod_meta.get("itemid", ""))
    install = mod_meta.get("install") or {"type": "copy", "params": {}}
    files = mod_meta.get("files") or []
    if not modid:
        raise StoreError("缺少 Mod ID")
    if not files:
        raise StoreError("服务端暂无可下载文件（可能尚未下载该 Mod）")

    staging = Path(local_path) / ".staging"
    staging.mkdir(parents=True, exist_ok=True)

    # 1) 下载（保留服务端呈现名作为 staging 相对路径）
    total_bytes = 0     # 解压后的原始字节（等效数据量）
    wire_total = 0      # 网络接收字节（累计到整个 Mod，跨文件累加）
    for f in files:
        name = f.get("name", "")
        if not name:
            continue
        dest = staging / name
        base_wire = wire_total
        box = {"wire": 0}      # 记录本文件当前的网络字节数

        def _traffic(w, _base=base_wire, _box=box):
            _box["wire"] = w
            if traffic_cb:
                traffic_cb(_base + w)

        try:
            done = api.download_file(
                server_url, appid, name, dest,
                progress_cb=(lambda d, t, n=name: progress_cb(n, d, t)) if progress_cb else None,
                compress=compress,
                traffic_cb=_traffic if traffic_cb else None,
            )
        except api.ApiError as e:
            raise StoreError(f"下载失败 {name}: {e}") from e
        total_bytes += done
        wire_total = base_wire + box["wire"]

    # 2) 安装（按类型分发）
    handler = get_handler(install.get("type", "copy"))
    ctx = InstallContext(
        staging_dir=staging,
        modid=modid,
        target_root=Path(local_path),
        params=install.get("params") or {},
        files=[f.get("name", "") for f in files],
    )
    try:
        result = handler.install(ctx)
    except Exception as e:  # noqa: BLE001
        raise StoreError(f"安装失败 ({handler.type_key}): {e}") from e

    # 3) 记录状态（先取出上次产物，便于清理旧的）
    state = load_state(local_path)
    old_artifacts = list((state.get(modid) or {}).get("artifacts", []))
    state[modid] = {
        "type": install.get("type", "copy"),
        "artifacts": list(result.artifacts),
        "installed_at": int(time.time()),
        "name": mod_meta.get("name", ""),
    }
    save_state(local_path, state)

    # 4) 清理上次安装遗留、且本次未再产出的旧产物
    #    （典型：target_ext 变更后，旧扩展名文件如 <modid>.bin 应被移除）
    new_set = set(str(a) for a in result.artifacts)
    root = Path(local_path)
    for a in old_artifacts:
        if str(a) in new_set:
            continue
        p = root / a
        try:
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
            elif p.is_file():
                p.unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            pass

    # 4) 清理该 Mod 的 staging 残留
    _cleanup_staging(staging, modid)

    return {
        "bytes": total_bytes,
        "artifacts": list(result.artifacts),
        "message": result.message,
        "type": install.get("type", "copy"),
    }


def _cleanup_staging(staging: Path, modid: str):
    """移除 staging 中该 Mod 的残留（<modid>/ 目录与 <modid>.* 文件）。"""
    d = staging / modid
    if d.is_dir():
        shutil.rmtree(d, ignore_errors=True)
    for p in staging.glob(f"{modid}.*"):
        if p.is_file():
            p.unlink(missing_ok=True)


def _guess_artifacts(root: Path, modid: str) -> list:
    """无状态记录时的兜底产物路径（目录 + 常见单文件）。"""
    out = [f"{modid}/"]
    for p in root.glob(f"{modid}.*"):
        if p.is_file():
            out.append(p.name)
    return out


def delete_mods(local_path: str, itemids) -> list:
    """本地删除指定 Mod 的安装产物，返回实际删除的 itemid 列表。"""
    state = load_state(local_path)
    root = Path(local_path)
    removed = []
    for iid in itemids:
        iid = str(iid)
        info = state.pop(iid, None)
        artifacts = info.get("artifacts") if info else None
        if not artifacts:
            artifacts = _guess_artifacts(root, iid)
        existed = False
        for a in artifacts:
            p = root / a
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
                existed = True
            elif p.is_file():
                p.unlink(missing_ok=True)
                existed = True
        if info is not None or existed:
            removed.append(iid)
    save_state(local_path, state)
    # 清理空的 staging（可选）
    staging = root / ".staging"
    if staging.is_dir() and not any(staging.iterdir()):
        staging.rmdir()
    return removed
