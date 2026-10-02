#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
客户端配置

- 硬编码 AppID：客户端启动前固定写入，仅拉取该游戏的 Mod（不可在 UI 修改）
- 已发现服务端地址（优先）：通过广播发现后持久化到 JSON（last_server_url），
  只要本地存在保存的地址就优先使用；其不可用时才临时启用广播
- 硬编码默认服务端地址：仅当本地“没有任何保存地址”时才使用
- 运行期配置（本地安装路径 + 已发现地址）保存到 JSON 文件，便于持久化
"""

import json
import os
import sys
from pathlib import Path

# ==================== 硬编码配置（启动前写入，仅此一个游戏） ====================
APPID = "550"          # 目标游戏 AppID（改这里即可切换游戏，仅拉取该游戏的 Mod）
GAME_NAME = ""            # 可选：游戏名留空则从服务端自动获取

# 服务端广播端口（与服务端 broadcast_port 保持一致）
BROADCAST_PORT = 37021
BROADCAST_TIMEOUT = 15    # 启动首次连接时等待广播的最长时间（秒）
RECHECK_INTERVAL = 10     # 后台地址复核 / 自动重连间隔（秒）

# 硬编码默认服务端地址（兜底：仅当本地没有任何保存地址时使用）
# 本地存在保存的发现地址(last_server_url)时优先使用保存地址；
# 保存地址不可用时才启用广播重新发现。
DEFAULT_SERVER_URL = "http://192.168.100.100:28080"

DEFAULT_LOCAL_PATH = ""   # 本地安装路径（留空需在设置中指定）

# 配置文件（保存 local_path 与 last_server_url：
#  - local_path      本地安装路径
#  - last_server_url 上次通过广播发现并连接成功后的服务端地址（优先使用），
#                    仅在其不存在时才回落到硬编码默认地址；服务端地址不再手工填写
CONFIG_FILE_NAME = "client_config.json"
FROZEN_APP_DIR = "GameSyncClient"   # 打包后回落到 %APPDATA% 时使用的子目录名


def _is_frozen() -> bool:
    """是否运行在打包后的 exe 中（Nuitka 置 sys.frozen / 提供 __compiled__）。"""
    return bool(getattr(sys, "frozen", False)) or ("__compiled__" in globals())


def _frozen_exe_dir() -> Path:
    """打包后真实的 exe 所在目录。

    注意（onefile 已实测）：
      - sys.executable -> %TEMP%\\onefile_*\\python.exe （临时解压目录，**不可用**）
      - __file__       -> %TEMP%\\onefile_*\\xxx.py     （临时解压目录，**不可用**）
      - __compiled__.containing_dir -> 真实 exe 目录（首选）
      - sys.argv[0]                 -> 真实 exe 路径（次选）
    standalone 模式下 sys.executable 即真实 exe，作为最后兜底。
    """
    compiled = globals().get("__compiled__")
    d = getattr(compiled, "containing_dir", None)
    if d:
        return Path(d)
    for cand in (sys.argv[0] if sys.argv else "", getattr(sys, "executable", "")):
        try:
            p = Path(cand).resolve()
            if p.is_file():
                return p.parent
        except Exception:  # noqa: BLE001
            continue
    return Path.cwd()


def _default_config_path() -> Path:
    """配置文件的存放位置。

    源码运行：项目根（client/ 的上一级），行为不变。

    打包后（Nuitka onefile）：绝不能用 __file__ / sys.executable —— 它们指向
    每次启动都会重建的临时解压目录（%TEMP%\\onefile_<pid>_<随机>），写进去的
    配置退出即丢（症状：“保存成功，重启后没了”）。故用真实 exe 目录
    （便携、用户可见），不可写时回落 %APPDATA%\\<FROZEN_APP_DIR>\\。
    """
    if not _is_frozen():
        return Path(__file__).resolve().parent.parent / CONFIG_FILE_NAME
    try:
        exe_dir = _frozen_exe_dir()
        if os.access(exe_dir, os.W_OK):
            return exe_dir / CONFIG_FILE_NAME
    except Exception:  # noqa: BLE001
        pass
    base = os.environ.get("APPDATA") or str(Path.home())
    d = Path(base) / FROZEN_APP_DIR
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception:  # noqa: BLE001
        pass
    return d / CONFIG_FILE_NAME


CONFIG_FILE = _default_config_path()


def config_path() -> str:
    """当前配置文件路径（便于日志/排障展示）。"""
    return str(CONFIG_FILE)


def load_runtime_config() -> dict:
    """读取运行期配置；文件不存在或损坏时返回默认值。"""
    cfg = {
        "local_path": DEFAULT_LOCAL_PATH,
        "last_server_url": "",
    }
    try:
        if CONFIG_FILE.exists():
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                if data.get("local_path"):
                    cfg["local_path"] = str(data["local_path"])
                if data.get("last_server_url"):
                    cfg["last_server_url"] = str(data["last_server_url"]).rstrip("/")
    except Exception:  # noqa: BLE001
        pass
    return cfg


def save_runtime_config(local_path: str, last_server_url: str = None):
    """持久化运行期配置。

    last_server_url 为 None 时保留文件中已有的该字段（避免保存本地路径时
    把已发现地址误清空）；传空字符串则显式清空。
    """
    existing = load_runtime_config()
    if last_server_url is None:
        last_server_url = existing.get("last_server_url", "")
    data = {
        "local_path": local_path or "",
        "last_server_url": (last_server_url or "").rstrip("/"),
    }
    try:
        CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:  # noqa: BLE001
        print(f"[警告] 保存客户端配置失败: {e}")


def load_saved_url() -> str:
    """读取持久化的“上次发现地址”（P1）；不存在则返回空串。"""
    return load_runtime_config().get("last_server_url", "")


def save_last_server_url(url: str):
    """保存通过广播发现并连接成功后的服务端地址。"""
    rt = load_runtime_config()
    save_runtime_config(rt["local_path"], (url or "").rstrip("/"))
