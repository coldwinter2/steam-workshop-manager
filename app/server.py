#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
FastAPI 主服务

提供：
  - Web 管理后台鉴权（账号密码登录，会话 Cookie）
  - 单页 Web 管理界面（仪表盘 / 游戏 / Mod / 设置 / 同步）
  - REST API 管理配置、触发同步、查看清单
  - Steam 账号登录测试（含设备授权 / Guard 码处理）
  - 文件分发接口（/list、/files/{game}、/download，兼容 demo 客户端，公开）
"""

import sys
import time
import zlib
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from .auto_updater import AutoUpdater
from .broadcast import BroadcastService
from . import ansi as ansi_module
from .config_manager import ConfigError, ConfigManager
from .file_dist import (
    build_manifest,
    files_for_game,
    list_projects,
    resolve_download,
    resolve_file,
)
from .install import available_types, get_install_type
from . import proxy as proxy_module
from .security import new_session_token
from .steamcmd_runner import SteamCMD
from .steam_meta import (
    SteamMetaError,
    configure_proxy,
    fetch_game_name,
    fetch_mod_name,
    parse_mod_url,
)
from .sync_manager import SyncManager

# ------------------------- 全局对象 -------------------------
CONFIG_PATH = "config.toml"
config = ConfigManager(CONFIG_PATH)
# 服务端自身日志颜色策略（auto: 仅 TTY 上色 / always / never）。注意：任务日志
# 已经被 steamcmd_runner 在捕获时统一去色，这里只决定 banner 等启动信息是否上色。
LOG_COLOR = config.get_log_color()


def _resolve_log_color():
    global LOG_COLOR
    LOG_COLOR = config.get_log_color()


def cprint(text: str, *colors: str, stream=None):
    """按 LOG_COLOR 策略输出（仅 TTY / always 时上色，never 或管道时纯文本）。"""
    target = stream or sys.stderr
    target.write(ansi_module.colored(text, *colors, mode=LOG_COLOR, stream=target) + "\n")


sync_manager = SyncManager(config)
auto_updater = AutoUpdater(config, sync_manager)
# 任何更新任务完成（自动触发 / 界面按钮 / 更新全部 / 单 Mod 更新）都回调
# 自动更新调度器重置计时，保证界面「下次自动更新」时间从完成时刻重新起算
sync_manager.set_finish_hook(auto_updater.on_sync_finished)
broadcast = None
STARTUP_SETTINGS = config.get_settings()

HTML_PATH = Path(__file__).parent / "templates" / "index.html"
with open(HTML_PATH, "r", encoding="utf-8") as _f:
    INDEX_HTML = _f.read()

FAVICON_PATH = Path(__file__).parent / "static" / "favicon.svg"

# 会话表：token -> {username, created}
SESSIONS: dict[str, dict] = {}
SESSION_MAX_AGE = 60 * 60 * 24 * 7  # 7 天


def _effective_proxy() -> dict:
    """返回当前生效的代理配置（config.toml + 环境变量覆盖）。"""
    return proxy_module.resolve_config(config.get_proxy())


# 启动时配置 Steam 网页访问的代理（使 steam_meta 后续请求经代理）
configure_proxy(_effective_proxy())


# ------------------------- 鉴权 -------------------------
def require_auth(request: Request) -> str:
    auth = config.get_auth()
    if not auth["enabled"]:
        return "anonymous"
    token = request.cookies.get("session")
    if not token or token not in SESSIONS:
        raise HTTPException(status_code=401, detail="未登录或会话已过期")
    return SESSIONS[token]["username"]


# ------------------------- 生命周期 -------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    global broadcast
    s = config.get_settings()
    # 自动更新调度器（配置持久化于 config.toml 的 [auto_update]）
    auto_updater.start()
    if config.get_auto_update()["enabled"]:
        cprint("自动更新调度已启动", "green")
    if s["enable_broadcast"]:
        broadcast = BroadcastService(
            http_port=STARTUP_SETTINGS["port"],
            broadcast_port=s["broadcast_port"],
            interval=s["broadcast_interval"],
        )
        broadcast.start()
        cprint(f"局域网广播已启动，端口: {s['broadcast_port']}", "cyan")
    yield
    auto_updater.stop()
    if broadcast:
        broadcast.stop()


app = FastAPI(title="Steam Mod 同步工具", version="1.1.0", lifespan=lifespan)

# 受保护路由：所有 /api/* 数据接口均需要登录
api = APIRouter(dependencies=[Depends(require_auth)])


# ------------------------- 请求模型 -------------------------
class GameIn(BaseModel):
    appid: str
    name: str = ""
    description: str = ""
    install_type: str = None
    install_params: dict = Field(default_factory=dict)


class GameUpdate(BaseModel):
    name: str = None
    description: str = None


class ModIn(BaseModel):
    mod_input: str
    name: str = ""


class SettingsIn(BaseModel):
    steamcmd_path: str = None
    storage_dir: str = None
    host: str = None
    port: int = None
    enable_broadcast: bool = None
    broadcast_port: int = None
    broadcast_interval: int = None
    proxy: dict = None
    log_color: str = None


class AuthIn(BaseModel):
    username: str = None
    password: str = None
    enabled: bool = None


class AutoUpdateIn(BaseModel):
    enabled: bool = None
    delay_enabled: bool = None
    delay_minutes: float = None
    scan_enabled: bool = None
    scan_interval: float = None


class SteamIn(BaseModel):
    enabled: bool = None
    username: str = None
    password: str = None
    guard_code: str = None


class LoginIn(BaseModel):
    username: str
    password: str


# ------------------------- 公开：登录 / 登出 / 分发 -------------------------
@app.post("/api/login")
def api_login(body: LoginIn, response: Response):
    if not config.verify_auth(body.username, body.password):
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    token = new_session_token()
    SESSIONS[token] = {"username": body.username, "created": time.time()}
    response.set_cookie(
        "session", token, httponly=True, samesite="lax", max_age=SESSION_MAX_AGE
    )
    return {"ok": True, "username": body.username}


@app.post("/api/logout")
def api_logout(response: Response, request: Request):
    token = request.cookies.get("session")
    if token and token in SESSIONS:
        del SESSIONS[token]
    response.delete_cookie("session")
    return {"ok": True}


@app.get("/list")
def dist_list():
    return {"projects": list_projects(config), "count": len(list_projects(config))}


@app.get("/mods/{appid}")
def dist_mods(appid: str):
    """公开：分组清单，modid -> {元数据 + 安装方式 + 文件列表}。

    客户端据此展示 Mod、计算依赖闭包、并按安装方式执行下载后安装。
    文件列表按安装方式呈现：copy 原样 / rename 单文件改名 / extract 单文件 zip。
    """
    manifest = build_manifest(config, sync_manager, appid)
    if manifest is None:
        raise HTTPException(status_code=404, detail=f"游戏不存在: {appid}")
    return manifest


@app.get("/install/types")
def dist_install_types():
    """公开：可用安装类型（供前端下拉/客户端映射处理器）。"""
    return {"types": available_types()}


@app.get("/files/{game}")
def dist_files(game: str):
    data = files_for_game(sync_manager, game)
    if data is None:
        raise HTTPException(status_code=404, detail=f"项目不存在或未下载: {game}")
    return data


# ------------------------- 下载压缩传输（gzip） -------------------------
# 已是高压缩比格式，再 gzip 几乎无收益、只增 CPU，故跳过
_NO_GZIP_EXT = {
    ".zip", ".gz", ".bz2", ".xz", ".7z", ".rar",
    ".png", ".jpg", ".jpeg", ".gif", ".webp",
    ".mp4", ".webm", ".avi", ".mov",
    ".mp3", ".ogg", ".flac", ".aac",
}


def _gzip_beneficial(path: Path) -> bool:
    """该文件是否值得 gzip 压缩传输。"""
    return path.suffix.lower() not in _NO_GZIP_EXT


def _gzip_stream(path: Path, chunk_size: int = 64 * 1024, expected_size: int = None):
    """流式 gzip 压缩文件内容（生成器，供 StreamingResponse 消费）。

    用 zlib.compressobj(..., 16 + zlib.MAX_WBITS) 产出标准 gzip 流，
    客户端可用 zlib.decompressobj(16 + zlib.MAX_WBITS) 还原（与 demo 一致）。

    异常处理：响应头（200）一旦发出就无法再改状态码，因此**不能静默
    return**——那样客户端只会收到一个缺少 gzip 尾部的截断流，可能被当成
    正常文件写盘。这里改为：记录可诊断的日志后**重新抛出**，让连接中断
    且服务端留痕。

    expected_size 为响应前 stat 到的大小：生成器结束时校验实际读到的字节数，
    不一致（文件在传输中被改写/截断）时报错，避免把半截内容当成完整文件下行。
    """
    compressor = zlib.compressobj(-1, zlib.DEFLATED, 16 + zlib.MAX_WBITS)
    read_bytes = 0
    try:
        with open(path, "rb") as f:
            while True:
                chunk = f.read(chunk_size)
                if not chunk:
                    break
                read_bytes += len(chunk)
                data = compressor.compress(chunk)
                if data:
                    yield data
        tail = compressor.flush()
        if tail:
            yield tail
    except Exception as e:  # noqa: BLE001
        # 已发出 200 头，无法再回错误码：写入日志后抛出，让 uvicorn/日志留痕，
        # 客户端因 gzip 尾部缺失而解压失败（不会静默写坏文件）
        print(f"[错误] 压缩传输中断 {path}：已读 {read_bytes}"
              f"{'/' + str(expected_size) if expected_size is not None else ''} 字节，"
              f"{type(e).__name__}: {e}", file=sys.stderr)
        raise
    if expected_size is not None and read_bytes != expected_size:
        # 文件在传输期间被改写/删除：gzip 流本身是自洽的，客户端无从察觉，
        # 必须靠服务端记账发现并置为失败
        raise OSError(f"压缩传输不完整 {path}：已读 {read_bytes} 字节，"
                      f"预期 {expected_size} 字节")


@app.get("/download/{game}/{file_path:path}")
def dist_download(game: str, file_path: str, request: Request):
    """按安装方式下行：rename 给改名后的内容、extract 给 zip、copy 原样。

    压缩传输：客户端在请求头带 `Accept-Encoding: gzip` 即启用压缩下行
    （服务端对可压缩文件流式 gzip，并回 `Content-Encoding: gzip` +
    `X-Original-Size`=压缩前大小供进度显示）；不带头则返回原始文件，
    与旧客户端/demo 完全兼容。
    """
    resolved = resolve_download(config, sync_manager, game, file_path)
    if resolved is None:
        raise HTTPException(status_code=404, detail="文件不存在")
    target, served_name = resolved

    accept = (request.headers.get("Accept-Encoding") or "").lower()
    # 断点续传优先级高于压缩：gzip 流的长度无法预知（只能分块传输、无
    # Content-Length），也无法按 Range 定位到原始字节偏移（压缩是有状态的，
    # 必须从文件头开始压）。因此一旦请求带 Range，就降级为原始文件响应
    # （FileResponse 支持 206 / Content-Range / Content-Length），保住续传能力。
    if (request.headers.get("Range") or "").strip():
        return FileResponse(str(target), filename=served_name)

    if "gzip" in accept and _gzip_beneficial(target):
        size = target.stat().st_size
        headers = {
            "Content-Encoding": "gzip",
            "X-Original-Size": str(size),
            "Content-Disposition": f'attachment; filename="{served_name}"',
            "Cache-Control": "no-store",
            # 明确声明不支持 Range，避免客户端误以为可续传：
            # 本响应为 chunked（无 Content-Length），且 gzip 无法随机定位
            "Accept-Ranges": "none",
        }
        return StreamingResponse(
            _gzip_stream(target, expected_size=size),
            media_type="application/octet-stream",
            headers=headers,
        )
    return FileResponse(str(target), filename=served_name)


# ------------------------- 状态 -------------------------
def _last_sync_info() -> dict:
    finished = [t for t in sync_manager.tasks.values() if t.status == "finished"]
    if not finished:
        return {"last_sync_at": None, "last_sync_task": None}
    latest = max(finished, key=lambda t: t.finished_at or 0)
    ok = sum(1 for r in latest.results if r.get("ok"))
    return {
        "last_sync_at": latest.finished_at,
        "last_sync_task": latest.task_id,
        "last_sync_ok": ok,
        "last_sync_fail": len(latest.results) - ok,
    }


@api.get("/status")
def api_status():
    s = config.get_settings()
    proxy_cfg = _effective_proxy()
    steam = SteamCMD(s["steamcmd_path"])
    acct = config.get_steam_account()
    games = config.get_games()
    total_mods = sum(len(g["mods"]) for g in games)
    manifest = sync_manager.scan_manifest()
    return {
        "steamcmd_path": s["steamcmd_path"],
        "steamcmd_exists": steam.exists(),
        "storage_dir": str(sync_manager.storage_abs()),
        "storage_exists": sync_manager.storage_abs().exists(),
        "bound_host": STARTUP_SETTINGS["host"],
        "bound_port": STARTUP_SETTINGS["port"],
        "configured_games": len(games),
        "configured_mods": total_mods,
        "downloaded_mods": len(manifest),
        "sync_running": sync_manager.current_task_id() is not None,
        "current_task": sync_manager.current_task_id(),
        "auth_enabled": config.get_auth()["enabled"],
        "log_color": config.get_log_color(),
        "proxy": {
            "enabled": bool(proxy_cfg["enabled"]),
            "type": proxy_cfg["type"],
            "endpoint": f"{proxy_cfg['host']}:{proxy_cfg['port']}",
        },
        "steam_account": {
            "enabled": acct["enabled"],
            "username": acct["username"],
            "has_password": bool(acct["password"]),
            "has_guard_code": bool(acct["guard_code"]),
        },
        "auto_update": {
            "enabled": config.get_auto_update()["enabled"],
            "pending_count": len(sync_manager.pending_targets()),
        },
        **_last_sync_info(),
    }


@api.get("/config")
def api_get_config():
    return {"settings": config.get_settings(), "games": config.get_games()}


# ------------------------- 游戏 -------------------------
@api.post("/games", status_code=201)
def api_add_game(g: GameIn):
    try:
        # 名称留空时，尝试从 Steam 自动获取（失败不阻断添加）
        name = g.name
        if not name:
            try:
                appid_norm = config.normalize_game_input(g.appid)
                name = fetch_game_name(appid_norm)
            except SteamMetaError:
                name = ""
        config.add_game(g.appid, name, g.description)
        # 可选：设置该游戏的安装方式
        if g.install_type:
            if get_install_type(g.install_type).key != str(g.install_type):
                raise HTTPException(status_code=400, detail=f"未知安装类型: {g.install_type}")
            appid_norm = config.normalize_game_input(g.appid)
            config.set_game_install(appid_norm, g.install_type, g.install_params)
    except ConfigError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "name": name}


@api.get("/steam/game/{appid}")
def api_steam_game_name(appid: str):
    """从 Steam 商店页获取游戏名称（自动填充）。"""
    try:
        norm = config.normalize_game_input(appid)
    except ConfigError as e:
        raise HTTPException(status_code=400, detail=f"无法解析 AppID: {e}")
    try:
        name = fetch_game_name(norm)
    except SteamMetaError as e:
        raise HTTPException(status_code=502, detail=f"获取游戏名称失败: {e}")
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"获取游戏名称失败: {e}")
    return {"appid": norm, "name": name}


@api.get("/steam/mod")
def api_steam_mod_meta(mod_input: str = ""):
    """按 Mod ID 或**创意工坊链接**获取名称（自动填充）。

    独立于此前的 /steam/mod/{itemid}：那里的路径参数无法承载完整链接
    （链接含 `/`，即便前端 encodeURIComponent 编码为 %2F，Starlette 仍会
    把它当分隔符参与路由匹配 -> 直接 404），因此链接必须从**查询参数**传，
    这里统一由 parse_mod_url 解析。
    """
    if not mod_input.strip():
        raise HTTPException(status_code=400, detail="请输入 Mod ID 或创意工坊链接")
    try:
        itemid = parse_mod_url(mod_input)
    except SteamMetaError as e:
        raise HTTPException(status_code=400, detail=str(e))
    try:
        name = fetch_mod_name(itemid)
    except SteamMetaError as e:
        raise HTTPException(status_code=502, detail=f"获取 Mod 名称失败: {e}")
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"获取 Mod 名称失败: {e}")
    return {"itemid": itemid, "name": name}


@api.get("/steam/mod/{itemid}")
def api_steam_mod_name(itemid: str):
    """从 Steam 创意工坊获取 Mod 名称（自动填充）。Mod ID 为纯数字时使用。"""
    # 校验为数字 ID
    if not itemid.isdigit():
        try:
            from .steam_meta import parse_mod_url
            itemid = parse_mod_url(itemid)
        except SteamMetaError as e:
            raise HTTPException(status_code=400, detail=str(e))
    try:
        name = fetch_mod_name(itemid)
    except SteamMetaError as e:
        raise HTTPException(status_code=502, detail=f"获取 Mod 名称失败: {e}")
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"获取 Mod 名称失败: {e}")
    return {"itemid": itemid, "name": name}


@api.put("/games/{appid}")
def api_update_game(appid: str, u: GameUpdate):
    try:
        config.update_game(appid, u.name, u.description)
    except ConfigError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return {"ok": True}


class InstallUpdate(BaseModel):
    type: str
    params: dict = Field(default_factory=dict)


@api.put("/games/{appid}/install")
def api_update_game_install(appid: str, body: InstallUpdate):
    """设置游戏级默认安装方式（rename / extract / copy / custom）。"""
    if get_install_type(body.type).key != str(body.type):
        raise HTTPException(status_code=400, detail=f"未知安装类型: {body.type}")
    try:
        config.set_game_install(appid, body.type, body.params)
    except ConfigError as e:
        raise HTTPException(status_code=404, detail=str(e))
    return {"ok": True, "install": config.get_game_install(appid)}


@api.delete("/games/{appid}")
def api_delete_game(appid: str):
    if sync_manager.current_task_id() is not None:
        raise HTTPException(status_code=409, detail="同步任务正在进行，请稍后再删除")
    try:
        config.delete_game(appid)
    except ConfigError as e:
        raise HTTPException(status_code=404, detail=str(e))
    # 配置删除成功后同步清理已下载文件（整个 <storage>/<appid> 目录）
    files = sync_manager.delete_game_files(appid)
    return {"ok": True, **files}


# ------------------------- Mod -------------------------
@api.post("/games/{appid}/mods", status_code=201)
def api_add_mod(appid: str, m: ModIn):
    try:
        # 自动解析 Steam 创意工坊依赖并一并添加（含主体名称自动获取）
        result = config.add_mod(appid, m.mod_input, m.name)
    except ConfigError as e:
        raise HTTPException(status_code=400, detail=str(e))
    # 新增 Mod -> 通知自动更新调度器（倒计时期间再次新增会合并并重新计时）
    try:
        added = 1 + len(result.get("added_dependencies") or [])
        auto_updater.notify_mods_added(appid, added)
    except Exception:  # noqa: BLE001 通知失败不影响添加结果
        pass
    return {"ok": True, **result}


@api.post("/games/{appid}/mods/{itemid}/update")
def api_update_mod(appid: str, itemid: str):
    """单独更新一个 Mod。"""
    try:
        task_id = sync_manager.start_update(appid, [itemid])
    except ConfigError as e:
        raise HTTPException(status_code=404, detail=str(e))
    if task_id is None:
        raise HTTPException(status_code=409, detail="已有同步任务正在进行")
    return {"ok": True, "task_id": task_id}


@api.post("/games/{appid}/update")
def api_update_game_mods(appid: str):
    """更新指定游戏下的所有 Mod。"""
    try:
        task_id = sync_manager.start_update(appid)
    except ConfigError as e:
        raise HTTPException(status_code=404, detail=str(e))
    if task_id is None:
        raise HTTPException(status_code=409, detail="已有同步任务正在进行")
    return {"ok": True, "task_id": task_id}


@api.delete("/games/{appid}/mods/{itemid}")
def api_delete_mod(appid: str, itemid: str):
    if sync_manager.current_task_id() is not None:
        raise HTTPException(status_code=409, detail="同步任务正在进行，请稍后再删除")
    try:
        result = config.delete_mod(appid, itemid)
    except ConfigError as e:
        raise HTTPException(status_code=404, detail=str(e))
    # 配置删除成功后，同步清理主体与级联孤儿依赖的已下载文件
    removed_ids = result["removed"] + result["removed_auto"]
    files = sync_manager.delete_mod_files(appid, removed_ids)
    # 关键：文件删除后必须再摘掉 ACF 里的下载记录，否则 steamcmd 认为「已下载」
    # 而跳过重下，导致该 Mod 再也下载不回来
    acf = sync_manager.sync_acf_records(appid, removed_ids)
    return {"ok": True, **result, **files, "acf": acf}


# ------------------------- 设置 -------------------------
@api.post("/settings")
def api_update_settings(s: SettingsIn):
    data = s.model_dump(exclude_none=True)
    config.update_settings(**data)
    # 代理配置变更后立即生效（无需重启）
    configure_proxy(_effective_proxy())
    if isinstance(data.get("log_color"), str):
        try:
            config.set_log_color(data["log_color"])
        except ConfigError as e:
            raise HTTPException(status_code=400, detail=str(e))
    _resolve_log_color()
    auto_updater.apply_config()
    return {"ok": True, "settings": config.get_settings(),
            "log_color": config.get_log_color()}


# ------------------------- 自动更新 -------------------------
@api.post("/auto_update/trigger")
def api_trigger_auto_update():
    """手动触发一次「仅未下载」的更新（界面按钮）。

    复用 auto_updater 的同一条触发路径（内部即 `sync_manager.start_pending()`），
    不新增重复的同步实现；仍受「同一时间只允许一个任务」约束。
    """
    r = auto_updater.trigger_now()
    if not r.get("ok"):
        # skipped=任务占用 409；其它启动失败 400
        raise HTTPException(
            status_code=409 if r.get("skipped") else 400,
            detail=r.get("message") or "触发失败",
        )
    return {"ok": True, **r}


@api.get("/auto_update")
def api_get_auto_update():
    """自动更新配置 + 运行时状态（下次触发时间 / 剩余倒计时 / 上次结果）。"""
    return {"ok": True, **auto_updater.status()}


@api.post("/auto_update")
def api_update_auto_update(body: AutoUpdateIn):
    """保存自动更新配置并立即应用（持久化到 config.toml，重启后仍生效）。"""
    data = body.model_dump(exclude_none=True)
    try:
        config.set_auto_update(**data)
    except ConfigError as e:
        raise HTTPException(status_code=400, detail=str(e))
    auto_updater.apply_config()
    return {"ok": True, **auto_updater.status()}


@api.post("/auth")
def api_update_auth(a: AuthIn):
    config.set_auth(username=a.username, password=a.password, enabled=a.enabled)
    return {"ok": True, "auth": {"enabled": config.get_auth()["enabled"],
                                 "username": config.get_auth()["username"]}}


@api.post("/steam")
def api_update_steam(s: SteamIn):
    config.set_steam_account(
        enabled=s.enabled, username=s.username,
        password=s.password, guard_code=s.guard_code,
    )
    acct = config.get_steam_account()
    return {
        "ok": True,
        "steam_account": {
            "enabled": acct["enabled"],
            "username": acct["username"],
            "has_password": bool(acct["password"]),
            "has_guard_code": bool(acct["guard_code"]),
        },
    }


@api.post("/steam/login")
def api_steam_login():
    acct = config.get_steam_account()
    if not acct["enabled"] or not acct["username"] or not acct["password"]:
        raise HTTPException(status_code=400, detail="请先在设置中配置并启用 Steam 账号")
    steam = SteamCMD(config.get_settings()["steamcmd_path"])
    if not steam.exists():
        raise HTTPException(status_code=400, detail="未找到 steamcmd，请检查路径")
    logs: list[str] = []
    result = steam.login_test(
        acct["username"], acct["password"], acct["guard_code"],
        log_callback=lambda line: logs.append(line),
    )
    return result


@api.post("/reload")
def api_reload():
    config.reload()
    configure_proxy(_effective_proxy())
    _resolve_log_color()
    auto_updater.apply_config()
    return {"ok": True, "settings": config.get_settings()}


# ------------------------- 同步 -------------------------
@api.post("/sync")
def api_start_sync():
    task_id = sync_manager.start_sync()
    if task_id is None:
        raise HTTPException(status_code=409, detail="已有同步任务正在进行")
    return {"ok": True, "task_id": task_id}


@api.get("/sync/current")
def api_sync_current():
    """当前在跑的任务；没有则回落到最近一次已完成的任务（供刷新后重建连接）。

    注意：必须定义在任何 `/sync/{task_id}` **之前**，否则会被参数化路由抢先
    匹配成 task_id="current" 而永远返回 404。
    """
    cur = sync_manager.current_task_id()
    last = sync_manager.last_finished()
    return {
        "task_id": cur or (last.task_id if last else None),
        "running": cur is not None,
        "last_task_id": last.task_id if last else None,
    }


@api.get("/sync/{task_id}")
def api_sync_status(task_id: str, since: int = 0):
    """任务状态 + 增量日志。

    `since` 为客户端已收到的最大日志序号（首次或刷新后传 0，拿到当前全部）：
      - 服务端只下发 seq > since 的条目，天然保序、去重；
      - 折叠行（进度 ×N）保持稳定行 id，客户端按 id 覆盖对应行即可，不重复；
      - 任务还在跑时返回 status=running，客户端继续用返回的 cursor 续拉；
      - 任务已结束返回 status=finished，并附 results/summary/error 供界面收尾。

    这样做的关键收益：**页面刷新、网络中断、切页都不会丢日志**——重连时带着
    上次的 cursor 就能补齐断点期间遗漏的全部输出。
    """
    task = sync_manager.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    return task.view(max(0, since))


@api.get("/manifest")
def api_manifest():
    return {"mods": sync_manager.scan_manifest()}


# ------------------------- 注册受保护路由 -------------------------
app.include_router(api, prefix="/api")


# ------------------------- 首页 -------------------------
@app.get("/", response_class=HTMLResponse)
def index():
    return HTMLResponse(INDEX_HTML)


# ------------------------- 站点图标 -------------------------
@app.get("/favicon.svg")
def favicon_svg():
    """站点图标（SVG 矢量，现代浏览器直接使用）。"""
    return FileResponse(str(FAVICON_PATH), media_type="image/svg+xml",
                        headers={"Cache-Control": "public, max-age=86400"})


@app.get("/favicon.ico")
def favicon_ico():
    """兼容浏览器默认的 /favicon.ico 请求（无 link 标签时的兜底）。

    返回同一份 SVG（带正确 MIME），避免 404；未支持 SVG 的极老浏览器会忽略。
    """
    return favicon_svg()


# ------------------------- 入口 -------------------------
def main():
    import uvicorn

    s = STARTUP_SETTINGS
    cprint(f"Steam Mod 同步工具已启动: http://{s['host']}:{s['port']}", "bold", "green")
    uvicorn.run(app, host=s["host"], port=s["port"], log_level="info")


if __name__ == "__main__":
    main()
