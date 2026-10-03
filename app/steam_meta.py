#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
从 Steam 获取元数据（游戏名称 / Mod 名称）

- 游戏名称：优先使用 Steam Store appdetails API（返回干净 JSON），
  失败则退回解析商店页 <title>
- Mod 名称 / 依赖：优先使用 ISteamRemoteStorage/GetPublishedFileDetails API，
  失败则退回解析创意工坊页 <title>；依赖项（dependencies）一并解析
- 同时提供 URL 解析：商店页地址 / 订阅链接 -> AppID / Mod ID

全部使用 requests 库（自动处理 gzip 解压、连接复用与超时），带异常重试与
兜底；网络不可用或解析失败时抛出 SteamMetaError，由调用方决定是否提示用户。
"""

import json
import os
import re
import time
import urllib.parse

import requests

from . import proxy as proxy_module

# PySocks 为 socks5 代理的可选依赖（requests 的 socks 支持由它提供）。
try:
    import socks  # noqa: F401
    _HAVE_SOCKS = True
except Exception:  # noqa: BLE001
    _HAVE_SOCKS = False

# 浏览器式 UA，避免被 Steam 直接拒绝
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
_HTTP_TIMEOUT = 20
_HTTP_RETRIES = 2          # 网络类失败的额外重试次数（代理链路常间歇性断连）
_RETRY_BACKOFF = 1.0       # 重试间隔基数（秒）：1s、2s…
# 重试的 HTTP 状态码：408/429 是 Steam 对短时间密集请求（级联拉依赖）的
# 常见响应，退避重试一次往往就成功；其余 4xx 属于请求本身问题，不重试。
_RETRYABLE_HTTP_CODES = {408, 429}

# 当前生效的代理配置（由 server 启动时调用 configure_proxy 注入）。
# 默认未启用代理，所有请求直连。
_PROXY_CFG = {"enabled": False, "type": "socks5", "host": "127.0.0.1",
              "port": 1080, "username": "", "password": ""}
# 复用的 Session：自动 gzip 解压、连接池复用，并只在显式启用代理时设置 proxies。
_SESSION = requests.Session()
_SESSION_CFG_KEY = None


def configure_proxy(cfg: dict | None = None):
    """设置生效的代理配置，使后续 Steam 网页/API 访问经该代理。

    传入的 cfg 应为 proxy.resolve_config(config.get_proxy()) 的结果
    （已合并 config.toml 与 环境变量）。传 None 表示禁用代理。
    """
    global _PROXY_CFG, _SESSION_CFG_KEY
    _PROXY_CFG = cfg or {"enabled": False}
    _SESSION_CFG_KEY = None  # 惰性重建


def _build_proxies(cfg: dict | None) -> dict:
    """把代理配置转换为 requests 所需的 proxies 字典。

    - 未启用：返回空字典。此时 requests 默认信任环境变量代理（trust_env=True），
      与 urllib 直连时同样会读取 *_proxy 环境变量的行为一致。
    - socks5/socks5h：需要 PySocks；缺失时告警并回退直连，避免 socks 支持
      是由旧版 requests 的兼容层提供而静默失效。
    - http/https：直接作为代理地址。
    """
    cfg = cfg or {}
    if not cfg.get("enabled"):
        return {}
    ptype = (cfg.get("type") or "socks5").lower()
    host, port = cfg.get("host"), int(cfg.get("port") or 0)
    if ptype in ("socks5", "socks5h"):
        if not _HAVE_SOCKS:
            print("[警告] 已启用 socks5 代理但未安装 PySocks，本次请求将直连。"
                  "请执行: pip install PySocks")
            return {}
        auth = ""
        if cfg.get("username"):
            auth = f"{cfg['username']}:{cfg.get('password') or ''}@"
        url = f"socks5h://{auth}{host}:{port}"
    elif ptype in ("http", "https"):
        url = f"{ptype}://{host}:{port}"
    else:
        return {}
    return {"http": url, "https": url}


def _session() -> requests.Session:
    """返回已按当前代理配置设置好 proxies 的 Session（配置未变则复用）。"""
    global _SESSION_CFG_KEY
    key = json.dumps(_PROXY_CFG, sort_keys=True, ensure_ascii=False)
    if key != _SESSION_CFG_KEY:
        # 未启用代理时清空 proxies：让 requests 依 trust_env 走环境变量/直连
        _SESSION.proxies = _build_proxies(_PROXY_CFG)
        _SESSION_CFG_KEY = key
    return _SESSION


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
            desc += f"（注意：环境变量存在 {envp}，未启用本功能代理时 requests 会使用它）"
    return desc


def _http_get(url: str, data: bytes = None, retries: int = None) -> str:
    """发起请求；网络类异常自动重试，最终统一包装为 SteamMetaError。

    - 重试原因：经代理访问 Steam 时，代理出口线路常出现**间歇性**失败
      （连接被立即关闭 / IncompleteRead / SSL EOF），重试一次往往即可成功。
    - 4xx 不重试（属于请求本身的问题）；5xx 与网络异常才重试。
    - 之前异常被上层静默吞掉只报「无法访问 Steam」，掩盖了真实原因，
      故统一在此包装并带上出口信息（走的哪个代理）。

    使用 requests：gzip 解压与连接复用由其内部处理，无需手工解压。
    """
    retries = _HTTP_RETRIES if retries is None else retries
    headers = {
        "User-Agent": _USER_AGENT,
        "Accept-Language": "zh-CN,zh;q=0.9",
    }
    session = _session()
    last_err: Exception | None = None
    for attempt in range(retries + 1):
        try:
            # POST 时 data 为 urlencoded bytes，此时需要显式声明 Content-Type
            hdrs = dict(headers)
            if data is not None:
                hdrs["Content-Type"] = "application/x-www-form-urlencoded"
            resp = session.request(
                "POST" if data is not None else "GET", url,
                data=data, headers=hdrs, timeout=_HTTP_TIMEOUT,
            )
            if resp.status_code < 500 and resp.status_code not in _RETRYABLE_HTTP_CODES:
                if resp.status_code >= 400:
                    raise SteamMetaError(
                        f"HTTP {resp.status_code}（{_via()}）: {url}"
                    )
                return resp.text
            last_err = requests.HTTPError(f"HTTP {resp.status_code}", response=resp)
        except SteamMetaError:
            raise
        except Exception as e:  # noqa: BLE001 requests.RequestException/OSError
            last_err = e
        if attempt < retries:
            # 429（Steam 限流）常见于级联拉取多个依赖，退避要更长才有效果
            factor = 3 if (isinstance(last_err, requests.HTTPError)
                           and last_err.response is not None
                           and last_err.response.status_code == 429) else 1
            time.sleep(_RETRY_BACKOFF * factor * (attempt + 1))
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
    """将用户输入（纯 Mod ID 或创意工坊链接）解析为 Mod ID。

    兼容形态：
      - 纯数字 ID：`123456789`
      - 标准订阅链接：`.../sharedfiles/filedetails/?id=123456789`
      - 带其它查询参数：`?id=123&searchtext=x`、`...&amp;id=123`（HTML 转义）
      - 百分号编码（前端 encodeURIComponent 后的整条链接）
      - 无 query 的短链：`.../sharedfiles/filedetails/123456789`

    注意：只要包含上述任一形态的第一段 ID 即返回，避免被后续参数干扰。
    """
    text = (text or "").strip()
    if not text:
        raise SteamMetaError("Mod 输入不能为空")
    if re.fullmatch(r"\d+", text):
        return text

    # 候选原文：原文 + 逐级百分号解码 + HTML 实体还原后的文本
    candidates = [text, text.replace("&amp;", "&")]
    cur = text
    for _ in range(2):
        cur = urllib.parse.unquote(cur)
        if cur not in candidates:
            candidates.append(cur)
    for c in candidates:
        m = re.search(r"[?&]id=(\d+)", c)
        if m:
            return m.group(1)
        m = re.search(r"(?:sharedfiles|workshop)/filedetails/(\d+)", c)
        if m:
            return m.group(1)
    raise SteamMetaError(
        "无法从输入中解析出 Mod ID"
        "（应为纯数字，或形如 https://steamcommunity.com/sharedfiles/filedetails/?id=<ID> 的创意工坊链接）"
    )


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
    except (SteamMetaError, requests.RequestException, ValueError, KeyError) as e:
        last_err = e  # 网络/解析失败都记下来，交给兜底步骤
    # 2) 商店页标题兜底
    try:
        html = _http_get(_STORE_PAGE.format(appid=appid))
    except (SteamMetaError, requests.RequestException) as e:
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


def _page_block_reason(html: str) -> str:
    """页面是否为「拿不到内容」的拦截页（而非正常的 Mod 详情页）。"""
    low = html.lower()
    if 'id="agecheck"' in low or "agegate" in low or "please enter your birth date" in low:
        return "Steam 返回了年龄验证页"
    if "you are currently logged out" in low or ("steamcommunity_login" in low
                                                 and "workshop" not in low):
        return "Steam 返回了登录页"
    return ""


def _extract_deps_from_page(html: str) -> tuple[list, bool]:
    """从创意工坊页的 <div id="RequiredItems"> 解析直接依赖 ID 列表。

    返回 (依赖 ID 列表, 是否找到 RequiredItems 区块)。两者必须区分开：
      - 找到区块但为空 -> 该 Mod 确实没有依赖；
      - 没找到区块     -> 页面结构变化/布局不同，**依赖未知**，必须提示，
                          不能当成「无依赖」静默通过。

    说明：ISteamRemoteStorage/GetPublishedFileDetails 接口的响应里
    **没有** dependencies/kids 字段（实测与官方文档均无），依赖项
    （Required Items）只能从工坊页解析——这也是主流开源工具的做法。
    """
    marker = html.find('id="RequiredItems"')
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
    return ids, marker >= 0


# 详情短时缓存：itemid -> (过期时间戳, 结果)
# 目的：级联拉取依赖时同一条目会被反复请求（同一 ID 出现在多个 Mod 的依赖里），
# 缓存能显著减少请求数，降低被 Steam 限流（429）导致依赖拉取失败的概率。
_DETAILS_CACHE: dict = {}
_CACHE_TTL = 300        # 秒
_CACHE_MAX = 256


def _details_cache_get(itemid: str):
    entry = _DETAILS_CACHE.get(str(itemid))
    if not entry:
        return None
    expire, value = entry
    if time.time() > expire:
        _DETAILS_CACHE.pop(str(itemid), None)
        return None
    return value


def _details_cache_put(itemid: str, value: dict):
    if len(_DETAILS_CACHE) >= _CACHE_MAX:
        _DETAILS_CACHE.pop(next(iter(_DETAILS_CACHE)), None)
    _DETAILS_CACHE[str(itemid)] = (time.time() + _CACHE_TTL, value)


def fetch_mod_details(itemid: str) -> dict:
    """获取创意工坊 Mod 的详情：名称与直接依赖项。

    返回 {"name": str, "dependencies": list[str], "warning": str}：
      - dependencies 为**已确认**的依赖列表；
      - warning 非空表示「依赖未能确认」，写明原因（页面被拦截 / 无依赖区块 /
        网络失败），调用方必须展示给用户，不能当成"该 Mod 没有依赖"静默通过。
    名称与依赖都拿不到时抛出 SteamMetaError（由调用方优雅处理）。

    结果带短时缓存：级联添加多个依赖时会重复请求同一个 Steam 域名，缓存能
    显著降低被限流（429）概率，同时也让同名 Mod 的重复添加更快。
    """
    cached = _details_cache_get(itemid)
    if cached is not None:
        return dict(cached)

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
    except (SteamMetaError, requests.RequestException, ValueError, KeyError,
            IndexError) as e:
        last_err = e
    # 2) 工坊页：解析 RequiredItems -> 依赖；<title> 兜底名称
    page_html = None
    try:
        page_html = _http_get(_MOD_PAGE.format(itemid=itemid))
    except (SteamMetaError, requests.RequestException) as e:
        last_err = e
        page_html = None
    warning = ""
    if page_html:
        blocked = _page_block_reason(page_html)
        if blocked:
            warning = f"依赖未知：{blocked}，无法读取 RequiredItems"
        else:
            page_deps, found = _extract_deps_from_page(page_html)
            if page_deps:
                deps = page_deps            # 页面是依赖的权威来源
            elif not found:
                # 区块不存在 = 依赖状态未知，绝不当成"无依赖"静默通过
                if "filedetails" in page_html:
                    warning = ("依赖未知：页面中未找到 RequiredItems 区块"
                               "（可能页面结构变化或不同语言布局），请手动确认依赖")
                else:
                    warning = "依赖未知：返回的页面不是 Mod 详情页（可能被重定向或已失效）"
        if not name:
            try:
                name = _clean_steam_title(_extract_title(page_html), itemid=itemid)
            except SteamMetaError as e:
                last_err = e
    if not page_html or not name:
        # 工坊页完全没拿到：名称+依赖都不可信，按失败处理并带上真实原因
        raise SteamMetaError(
            "无法访问 Steam 创意工坊页"
            + (f"：{last_err}" if last_err else "")
        )
    out = {"name": name or "", "dependencies": deps, "warning": warning}
    # 只有"确定拿到依赖"或"页面正常且确实无依赖"才缓存；警告态不缓存，
    # 便于下一次操作立即重试
    if not warning:
        _details_cache_put(itemid, out)
    return out


def fetch_mod_name(itemid: str) -> str:
    """获取创意工坊 Mod 名称；优先 API，失败退回页面标题。"""
    det = fetch_mod_details(itemid)
    if not det["name"]:
        raise SteamMetaError("未获取到 Mod 名称")
    return det["name"]
