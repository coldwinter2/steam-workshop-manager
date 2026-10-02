#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
从 Steam 获取元数据（游戏名称 / Mod 名称）

- 游戏名称：优先使用 Steam Store appdetails API（返回干净 JSON），
  失败则退回解析商店页 <title>
- Mod 名称 / 依赖：优先使用 ISteamRemoteStorage/GetPublishedFileDetails API，
  失败则退回解析创意工坊页 <title>；依赖项（dependencies）一并解析
- 同时提供 URL 解析：商店页地址 / 订阅链接 -> AppID / Mod ID

全部使用标准库 urllib，带超时与异常兜底；网络不可用或解析失败时
抛出 SteamMetaError，由调用方决定是否提示用户。
"""

import json
import os
import re
import time
import urllib.error
import zlib
import urllib.parse
import urllib.request
from pathlib import Path

from . import proxy as proxy_module

# 浏览器式 UA，避免被 Steam 直接拒绝
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
_HTTP_TIMEOUT = 20
_HTTP_RETRIES = 2          # 网络类失败的额外重试次数（代理链路常间歇性断连）
_RETRY_BACKOFF = 1.0       # 重试间隔基数（秒）：1s、2s…

# 当前生效的代理配置（由 server 启动时调用 configure_proxy 注入）。
# 默认未启用代理，所有请求直连。
_PROXY_CFG = {"enabled": False, "type": "socks5", "host": "127.0.0.1",
              "port": 1080, "username": "", "password": ""}
_OPENER = None


def configure_proxy(cfg: dict | None = None):
    """设置生效的代理配置，使后续 Steam 网页/API 访问经该代理。

    传入的 cfg 应为 proxy.resolve_config(config.get_proxy()) 的结果
    （已合并 config.toml 与 环境变量）。传 None 表示禁用代理。
    """
    global _PROXY_CFG, _OPENER
    _PROXY_CFG = cfg or {"enabled": False}
    _OPENER = None  # 惰性重建


def _get_opener():
    global _OPENER
    if _OPENER is None:
        _OPENER = proxy_module.build_opener(_PROXY_CFG)
    return _OPENER


_STORE_API = "https://store.steampowered.com/api/appdetails"
_STORE_PAGE = "https://store.steampowered.com/app/{appid}"
_MOD_API = "https://api.steampowered.com/ISteamRemoteStorage/GetPublishedFileDetails/v1/"
_MOD_PAGE = "https://steamcommunity.com/sharedfiles/filedetails/?id={itemid}"


class SteamMetaError(Exception):
    """Steam 元数据获取相关错误。"""


def _via() -> str:
    """当前出口描述，拼进错误信息，便于判断请求到底走没走代理。"""
    desc = proxy_module.describe(_PROXY_CFG)
    if not _PROXY_CFG.get("enabled"):
        envp = (os.environ.get("https_proxy") or os.environ.get("HTTPS_PROXY")
                or os.environ.get("http_proxy") or os.environ.get("HTTP_PROXY"))
        if envp:
            desc += f"（注意：环境变量存在 {envp}，未启用本功能代理时 urllib 会使用它）"
    return desc


def _http_get(url: str, data: bytes = None, retries: int = None) -> str:
    """发起请求；网络类异常自动重试，最终统一包装为 SteamMetaError。

    - 重试原因：经代理访问 Steam 时，代理出口线路常出现**间歇性**失败
      （连接被立即关闭 / IncompleteRead / SSL EOF），重试一次往往即可成功。
    - 4xx 不重试（属于请求本身的问题）；5xx 与网络异常才重试。
    - 之前异常被上层静默吞掉只报「无法访问 Steam」，掩盖了真实原因，
      故统一在此包装并带上出口信息（走的哪个代理）。
    """
    retries = _HTTP_RETRIES if retries is None else retries
    # 请求 gzip：Steam 页面/API 响应通常 20KB~120KB，压缩后往往只有几 KB。
    # 代理出口链路不稳定时，更小的响应体被中途截断的概率明显更低。
    headers = {
        "User-Agent": _USER_AGENT,
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Accept-Encoding": "gzip",
    }
    req = urllib.request.Request(url, data=data, headers=headers)
    opener = _get_opener()
    last_err: Exception | None = None
    for attempt in range(retries + 1):
        try:
            with opener.open(req, timeout=_HTTP_TIMEOUT) as resp:
                raw = resp.read()
                if (resp.headers.get("Content-Encoding") or "").lower() == "gzip":
                    raw = zlib.decompressobj(16 + zlib.MAX_WBITS).decompress(raw)
                charset = resp.headers.get_content_charset() or "utf-8"
                return raw.decode(charset, errors="replace")
        except urllib.error.HTTPError as e:
            if e.code < 500:
                raise SteamMetaError(f"HTTP {e.code}（{_via()}）: {url}") from e
            last_err = e
        except Exception as e:  # noqa: BLE001 URLError/SSLError/IncompleteRead/OSError
            last_err = e
        if attempt < retries:
            time.sleep(_RETRY_BACKOFF * (attempt + 1))
    raise SteamMetaError(
        f"访问失败（{_via()}，已重试 {retries} 次）"
        f"{type(last_err).__name__}: {last_err}"
    ) from last_err


def _extract_title(html: str) -> str:
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
    if not m:
        raise SteamMetaError("无法从页面解析标题")
    title = m.group(1).strip()
    # 基础 HTML 实体反转义
    title = (
        title.replace("&amp;", "&")
        .replace("&#39;", "'")
        .replace("&quot;", '"')
        .replace("&lt;", "<")
        .replace("&gt;", ">")
    )
    return title


def _clean_steam_title(title: str, appid: str = "", itemid: str = "") -> str:
    """清理 Steam 页面标题中的常见前后缀。"""
    t = title.strip()
    # 去掉商店/社区常见后缀
    suffixes = [
        " on Steam", " - Steam Community", "Steam Workshop::",
        "Steam Community :: ", " on Steam Community",
        "Steam 社区 :: ", " 在 Steam 上",
    ]
    for sfx in suffixes:
        if t.endswith(sfx):
            t = t[: -len(sfx)].strip()
    # 去掉折扣前缀：如 "Save 75% on X"
    m = re.match(r"^save\s+\d+%\s+on\s+(.+)$", t, re.IGNORECASE)
    if m:
        t = m.group(1).strip()
    # 兜底：仍为标题页或报错页
    if not t or "Welcome to Steam" in t or "Steam" == t:
        raise SteamMetaError("页面未返回有效名称")
    return t


# ------------------------- URL 解析 -------------------------
def parse_game_url(text: str) -> str:
    """将用户输入（纯 AppID 或商店页地址）解析为 AppID。"""
    text = (text or "").strip()
    if not text:
        raise SteamMetaError("游戏输入不能为空")
    if re.fullmatch(r"\d+", text):
        return text
    m = re.search(r"/app/(\d+)", text)
    if m:
        return m.group(1)
    m = re.search(r"[?&]app=(\d+)", text)
    if m:
        return m.group(1)
    raise SteamMetaError("无法从输入中解析出 AppID（应为纯数字或 Steam 商店页地址）")


def parse_mod_url(text: str) -> str:
    """将用户输入（纯 Mod ID 或订阅链接）解析为 Mod ID。"""
    text = (text or "").strip()
    if not text:
        raise SteamMetaError("Mod 输入不能为空")
    if re.fullmatch(r"\d+", text):
        return text
    m = re.search(r"[?&]id=(\d+)", text)
    if m:
        return m.group(1)
    raise SteamMetaError("无法从输入中解析出 Mod ID（应为纯数字或包含 ?id= 的订阅链接）")


# ------------------------- 名称获取 -------------------------
def fetch_game_name(appid: str) -> str:
    """获取 Steam 游戏名称；优先 API，失败退回页面标题。"""
    last_err: Exception | None = None
    # 1) appdetails API
    try:
        url = f"{_STORE_API}?appids={appid}"
        data = json.loads(_http_get(url))
        entry = data.get(str(appid)) or data.get(appid) or {}
        name = entry.get("data", {}).get("name")
        if name:
            return name
    except (SteamMetaError, urllib.error.URLError, urllib.error.HTTPError,
            ValueError, KeyError) as e:
        last_err = e  # 网络/解析失败都记下来，交给兜底步骤
    # 2) 商店页标题兜底
    try:
        html = _http_get(_STORE_PAGE.format(appid=appid))
    except (SteamMetaError, urllib.error.URLError, urllib.error.HTTPError) as e:
        # 保留真实原因（含走的出口：代理/直连），不再只报「无法访问」
        raise SteamMetaError(
            f"无法访问 Steam 商店页: {e}" + (f"；API 阶段错误: {last_err}" if last_err else "")
        ) from e
    return _clean_steam_title(_extract_title(html), appid=appid)


def _extract_deps(details: dict) -> list:
    """从 GetPublishedFileDetails 结果中提取依赖项 ID 列表。"""
    ids: list = []
    for raw in details.get("dependencies") or []:
        pid = (
            raw.get("publishedfileid")
            or raw.get("fileid")
            or raw.get("publishedfile_id")
        )
        if pid:
            ids.append(str(pid))
    for raw in details.get("kids") or []:
        pid = raw.get("publishedfileid") if isinstance(raw, dict) else raw
        if pid:
            ids.append(str(pid))
    # 去重并保持顺序
    seen = set()
    out = []
    for i in ids:
        if i not in seen:
            seen.add(i)
            out.append(i)
    return out


_DEPS_PAGE_RE = re.compile(r"(?:workshop|sharedfiles)/filedetails/\?id=(\d+)")


def _extract_deps_from_page(html: str) -> list:
    """从创意工坊页的 <div id="RequiredItems"> 解析直接依赖 ID 列表。

    说明：ISteamRemoteStorage/GetPublishedFileDetails 接口的响应里
    **没有** dependencies/kids 字段（实测与官方文档均无），依赖项
    （Required Items）只能从工坊页解析——这也是主流开源工具的做法。
    """
    marker = html.find('id="RequiredItems"')
    if marker < 0:
        return []
    # 定位该 div 的起始 <div
    dstart = html.rfind("<div", 0, marker)
    if dstart < 0:
        dstart = marker
    # 向后做 <div> 嵌套计数，截取到闭合 </div>
    end = min(len(html), dstart + 60000)
    depth = 0
    pos = dstart
    while pos < end:
        nxt_open = html.find("<div", pos)
        nxt_close = html.find("</div>", pos)
        if nxt_close == -1 or nxt_close > end:
            break
        if nxt_open != -1 and nxt_open < nxt_close:
            depth += 1
            pos = nxt_open + 4
        else:
            depth -= 1
            pos = nxt_close + 6
            if depth <= 0:
                end = nxt_close + 6
                break
    block = html[dstart:end]
    ids: list = []
    seen: set = set()
    for i in _DEPS_PAGE_RE.findall(block):
        if i not in seen:
            seen.add(i)
            ids.append(i)
    return ids


def fetch_mod_details(itemid: str) -> dict:
    """获取创意工坊 Mod 的详情：名称与直接依赖项。

    返回 {"name": str, "dependencies": list[str]}。
    - 名称：优先 GetPublishedFileDetails API 的 title，退回工坊页 <title>
    - 依赖：从工坊页 <div id="RequiredItems"> 解析（API 不返回依赖字段）
    完全无法访问 Steam 时抛出 SteamMetaError（由调用方优雅处理）。
    """
    deps: list = []
    name: str | None = None
    last_err: Exception | None = None
    # 1) GetPublishedFileDetails API（POST 表单）——拿名称
    try:
        post = urllib.parse.urlencode(
            {"itemcount": "1", "publishedfileids[0]": itemid}
        ).encode()
        data = json.loads(_http_get(_MOD_API, data=post))
        details = data["response"]["publishedfiledetails"][0]
        deps = _extract_deps(details)   # 兜底：万一响应含依赖字段
        name = details.get("title")
    except (SteamMetaError, urllib.error.URLError, urllib.error.HTTPError,
            ValueError, KeyError, IndexError) as e:
        last_err = e
    # 2) 工坊页：解析 RequiredItems -> 依赖；<title> 兜底名称
    page_html = None
    try:
        page_html = _http_get(_MOD_PAGE.format(itemid=itemid))
    except (SteamMetaError, urllib.error.URLError, urllib.error.HTTPError) as e:
        last_err = e
        page_html = None
    if page_html:
        page_deps = _extract_deps_from_page(page_html)
        if page_deps:
            deps = page_deps           # 页面是依赖的权威来源
        if not name:
            name = _clean_steam_title(_extract_title(page_html), itemid=itemid)
    if not name and not deps:
        raise SteamMetaError(
            "无法访问 Steam 创意工坊页"
            + (f"：{last_err}" if last_err else "")
        )
    return {"name": name or "", "dependencies": deps}


def fetch_mod_name(itemid: str) -> str:
    """获取创意工坊 Mod 名称；优先 API，失败退回页面标题。"""
    det = fetch_mod_details(itemid)
    if not det["name"]:
        raise SteamMetaError("未获取到 Mod 名称")
    return det["name"]
