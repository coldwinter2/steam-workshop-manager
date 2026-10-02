#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
服务端 API 客户端（只读）

客户端对服务端仅做 GET（拉取元数据 + 下载文件），绝不写入，
因此任何客户端操作都不会影响服务端文件。

依赖的公开接口（服务端均无需登录）：
  - GET /mods/{appid}      Mod 列表 + 依赖元数据
  - GET /files/{appid}     已下载文件清单（含大小）
  - GET /download/{appid}/{path}  文件内容
"""

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable, Optional

from .config import APPID

# 客户端只访问本地/局域网的 game-sync 服务端，不经过任何 HTTP 代理。
# （系统可能设置了 http_proxy，会把 localhost 请求错误转发到代理，故显式禁用。）
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class ApiError(Exception):
    """服务端访问错误。"""


def _get_json(url: str, timeout: float = 15):
    try:
        with _OPENER.open(url, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise ApiError(f"资源不存在: {url}") from e
        raise ApiError(f"HTTP {e.code}: {url}") from e
    except urllib.error.URLError as e:
        raise ApiError(f"无法连接服务端: {e.reason}") from e
    except Exception as e:  # noqa: BLE001
        raise ApiError(f"请求失败: {e}") from e


def ping(server_url: str, timeout: float = 5) -> bool:
    """轻量连通性探测：访问公开接口 /install/types，仅判断服务端是否可达。

    用于地址优选与自动重连（不拉清单，开销极小）。
    """
    url = f"{server_url.rstrip('/')}/install/types"
    try:
        with _OPENER.open(url, timeout=timeout) as resp:
            return 200 <= getattr(resp, "status", 200) < 300
    except Exception:  # noqa: BLE001
        return False


def get_mods(server_url: str, appid: str = None) -> dict:
    """获取分组清单（modid -> {元数据, 安装方式, 文件列表}）。

    文件列表按安装方式呈现：copy 为 modid/... 、rename 为 modid+ext、
    extract 为 modid.zip。安装方式供客户端分发安装处理器。
    """
    appid = appid or APPID
    url = f"{server_url.rstrip('/')}/mods/{urllib.parse.quote(str(appid))}"
    return _get_json(url)


def get_files(server_url: str, appid: str = None) -> dict:
    """获取扁平文件清单（demo 兼容，客户端当前用分组清单 get_mods）。"""
    appid = appid or APPID
    url = f"{server_url.rstrip('/')}/files/{urllib.parse.quote(str(appid))}"
    return _get_json(url)


def download_file(
    server_url: str,
    appid: str,
    rel_path: str,
    dest_path,
    progress_cb: Optional[Callable[[int, int], None]] = None,
    compress: bool = False,
    traffic_cb: Optional[Callable[[int], None]] = None,
    timeout: float = 60,
) -> int:
    """下载单个文件到 dest_path，返回写入字节数（解压后的原始大小）。

    traffic_cb(wire_bytes) 可选：上报**累计网络接收字节**（未解压的线上字节），
    用于压缩下载时同时展示「压缩传输速度（网络）」与「等效速度（解压后）」；
    常规下载两者相等。

    compress=False（默认，常规下载）：请求头 `Accept-Encoding: identity`，
    服务端直接返回原始文件，不做压缩，避免压缩 CPU 开销拖慢吞吐。
    compress=True（压缩下载）：请求头带 `Accept-Encoding: gzip`，由服务端
    流式压缩下行；仅当用户主动选择该方式时才启用。

    无论哪种方式，只要响应是 `Content-Encoding: gzip` 都流式解压后写盘
    （urllib 不会自动解压，需手动 zlib.decompressobj(16 + MAX_WBITS)），
    保证与现有下载流程兼容。
    进度总量优先取 `X-Original-Size`（压缩前大小），否则取 Content-Length。

    rel_path 为文件相对 content 目录的路径（如 "123456/mod.vpk"）。
    progress_cb(done_bytes, total_bytes) 用于进度显示（均为原始字节）。
    """
    from pathlib import Path
    import zlib

    encoded = "/".join(urllib.parse.quote(part) for part in rel_path.split("/"))
    url = f"{server_url.rstrip('/')}/download/{urllib.parse.quote(str(appid))}/{encoded}"

    dest_path = Path(dest_path)
    dest_path.parent.mkdir(parents=True, exist_ok=True)

    # 常规下载 = 明确要求 identity（原始文件直传）；压缩下载才请求 gzip
    accept = "gzip" if compress else "identity"
    req = urllib.request.Request(url, headers={"Accept-Encoding": accept})
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            encoding = (resp.headers.get("Content-Encoding") or "").lower()
            # 进度总量：压缩前原始大小优先（gzip 响应无 Content-Length）
            total = int(
                resp.headers.get("X-Original-Size")
                or resp.headers.get("Content-Length")
                or 0
            )
            done = 0      # 解压后写入的原始字节（等效数据量）
            wire = 0      # 实际网络接收字节（压缩后/原始线上字节）
            # 先写临时文件，成功后再替换，避免中断留下半个文件
            tmp = dest_path.with_suffix(dest_path.suffix + ".part")
            with open(tmp, "wb") as f:
                if "gzip" in encoding:
                    dec = zlib.decompressobj(16 + zlib.MAX_WBITS)
                    while True:
                        chunk = resp.read(64 * 1024)
                        if not chunk:
                            break
                        wire += len(chunk)
                        if traffic_cb:
                            traffic_cb(wire)
                        data = dec.decompress(chunk)
                        if data:
                            f.write(data)
                            done += len(data)
                            if progress_cb:
                                progress_cb(done, total)
                    tail = dec.flush()
                    if tail:
                        f.write(tail)
                        done += len(tail)
                        if progress_cb:
                            progress_cb(done, total)
                else:
                    while True:
                        chunk = resp.read(64 * 1024)
                        if not chunk:
                            break
                        wire += len(chunk)
                        if traffic_cb:
                            traffic_cb(wire)
                        f.write(chunk)
                        done += len(chunk)
                        if progress_cb:
                            progress_cb(done, total)
            # 截断防护：gzip 是流式压缩，服务端若中途失败/文件被改写，
            # 客户端拿到的仍可能是一个自洽但内容不全的 gzip 流。解压后的
            # 字节数与服务端声明的原始大小（X-Original-Size）不符即视为失败，
            # 绝不把不完整文件提升为正式文件。
            if "gzip" in encoding and total and done != total:
                tmp.unlink(missing_ok=True)
                raise ApiError(
                    f"下载不完整（解压后 {done}/{total} 字节）: {rel_path}")
            tmp.replace(dest_path)
            return done
    except urllib.error.HTTPError as e:
        raise ApiError(f"下载失败 HTTP {e.code}: {rel_path}") from e
    except urllib.error.URLError as e:
        raise ApiError(f"下载失败: {e.reason}") from e
    except Exception as e:  # noqa: BLE001
        raise ApiError(f"下载失败 {rel_path}: {e}") from e
