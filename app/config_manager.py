#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
配置管理模块

- 主配置 config.toml：settings / auth / steam_account / security / games 列表
- 每个游戏一个独立 TOML（games/<appid>.toml）存放其 Mod 列表
- Web 登录密码使用 PBKDF2 哈希；Steam 账号密码使用 Fernet 加密
- 使用 tomlkit 读写，保留文件注释与排版
"""

import re
from pathlib import Path

import tomlkit

from .security import (
    decrypt_secret,
    encrypt_secret,
    generate_fernet_key,
    hash_password,
    verify_password,
)
from .steam_meta import (
    SteamMetaError,
    fetch_game_name,
    fetch_mod_details,
    parse_game_url,
    parse_mod_url,
)

DEFAULT_ADMIN_USER = "admin"
DEFAULT_ADMIN_PASS = "admin123"

# 默认安装方式（原样分发，保留 <modid>/ 目录结构）
DEFAULT_INSTALL_TYPE = "copy"

# 依赖解析最大递归深度，避免异常深的依赖树拖垮添加操作
_MAX_DEP_DEPTH = 6


def _brief_reason(e: Exception) -> str:
    """把 Steam 元数据异常翻译成用户看得懂的中文原因。

    steam_meta 抛出的原文往往很长（含出口描述、重试次数、底层异常），
    这里按典型症状归类并保留原文摘要，便于界面直接展示。
    """
    msg = str(e).strip()
    low = msg.lower()
    rules = [
        ("429", "Steam 限流（429），好一会儿都拉不下来"),
        ("timed out", "网络超时"),
        ("timeout", "网络超时"),
        ("eof occurred", "TLS/代理链路中断（SSL EOF）"),
        ("incompleteread", "传输中断（响应体未读完）"),
        ("name or service not known", "域名解析失败"),
        ("getaddrinfo", "域名解析失败"),
        ("nodename nor servname", "域名解析失败"),
        ("connection refused", "连接被拒绝（目标端口不可达）"),
        ("connection reset", "连接被重置"),
        ("network is unreachable", "网络不可达"),
        ("no route to host", "网络不可达"),
        ("certificate", "证书校验失败"),
        ("proxy", "代理连接失败"),
        ("socks", "代理连接失败"),
        ("ssl", "TLS 握手失败"),
        ("403", "Steam 拒绝访问（403）"),
        ("401", "鉴权失败（401）"),
        ("http 5", "Steam 服务端错误"),
    ]
    for key, zh in rules:
        if key in low:
            return f"{zh}：{msg[:160]}"
    return msg[:200] or "未知原因"


class ConfigError(Exception):
    """配置相关错误。"""


class ConfigManager:
    """基于 TOML 的配置文件管理。"""

    def __init__(self, path: str = "config.toml"):
        self.path = Path(path)
        self.doc = self._load()
        self._ensure_defaults()

    # ------------------------- 底层加载 / 保存 -------------------------
    def _load(self):
        if self.path.exists():
            try:
                with open(self.path, "r", encoding="utf-8") as f:
                    return tomlkit.load(f)
            except Exception as e:  # noqa: BLE001
                print(f"[警告] 读取 {self.path} 失败，使用默认配置: {e}")
        return self._default_doc()

    def _default_doc(self):
        doc = tomlkit.document()
        settings = tomlkit.table()
        settings["steamcmd_path"] = "C:/steamcmd/steamcmd.exe"
        settings["storage_dir"] = "./mods"
        settings["host"] = "0.0.0.0"
        settings["port"] = 8080
        settings["enable_broadcast"] = True
        settings["broadcast_port"] = 37021
        settings["broadcast_interval"] = 5
        doc["settings"] = settings
        doc["auth"] = self._default_auth_table()
        doc["steam_account"] = self._default_steam_table()
        doc["games"] = tomlkit.aot()
        return doc

    @staticmethod
    def _default_auth_table():
        t = tomlkit.table()
        t["enabled"] = True
        t["username"] = DEFAULT_ADMIN_USER
        t["password"] = hash_password(DEFAULT_ADMIN_PASS)
        return t

    @staticmethod
    def _default_steam_table():
        t = tomlkit.table()
        t["enabled"] = False
        t["username"] = ""
        t["password"] = ""
        t["guard_code"] = ""
        return t

    @staticmethod
    def _default_proxy_table():
        t = tomlkit.table()
        t["enabled"] = False
        t["type"] = "socks5"
        t["host"] = "127.0.0.1"
        t["port"] = 1080
        t["username"] = ""
        t["password"] = ""
        return t

    @staticmethod
    def _default_auto_update_table():
        """自动更新默认配置（总开关默认关闭，避免意外自动下载）。"""
        t = tomlkit.table()
        t["enabled"] = False           # 总开关
        t["delay_enabled"] = True      # 触发条件一：新增 Mod 后延时触发
        t["delay_minutes"] = 5         # 延时时长（分钟）
        t["scan_enabled"] = True       # 触发条件二：定时扫描
        t["scan_interval"] = 5   # 扫描间隔（分钟）
        return t

    @staticmethod
    def _default_logging_table():
        """日志颜色策略（仅影响服务端自身输出，不影响已清洗的任务日志）。

        color:
          auto   —— 仅当标准错误是终端（TTY）时才上色，管道/journal/文件均不上色（默认）
          always —— 始终上色（仅在确实连到彩色终端时使用）
          never  —— 永远不上色（日志文件/journal/管道场景用这个彻底关闭）
        """
        t = tomlkit.table()
        t["color"] = "auto"
        return t

    def _ensure_defaults(self):
        changed = False
        if "auth" not in self.doc:
            self.doc["auth"] = self._default_auth_table()
            changed = True
        else:
            auth = self.doc["auth"]
            if "username" not in auth:
                auth["username"] = DEFAULT_ADMIN_USER
                changed = True
            if "password" not in auth or not auth["password"]:
                auth["password"] = hash_password(DEFAULT_ADMIN_PASS)
                changed = True
            if "enabled" not in auth:
                auth["enabled"] = True
                changed = True
        if "steam_account" not in self.doc:
            self.doc["steam_account"] = self._default_steam_table()
            changed = True
        if "proxy" not in self.doc:
            self.doc["proxy"] = self._default_proxy_table()
            changed = True
        if "auto_update" not in self.doc:
            self.doc["auto_update"] = self._default_auto_update_table()
            changed = True
        if "security" not in self.doc:
            sec = tomlkit.table()
            sec["key"] = generate_fernet_key()
            self.doc["security"] = sec
            changed = True
        else:
            if not self.doc["security"].get("key"):
                self.doc["security"]["key"] = generate_fernet_key()
                changed = True
        if "logging" not in self.doc:
            self.doc["logging"] = self._default_logging_table()
            changed = True
        else:
            lg = self.doc["logging"]
            if "color" not in lg:
                lg["color"] = "auto"
                changed = True
        if changed:
            self.save()

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            tomlkit.dump(self.doc, f)

    def reload(self):
        self.doc = self._load()
        self._ensure_defaults()
        return self.get_settings()

    # ------------------------- 安全密钥 -------------------------
    def fernet_key(self) -> str:
        return self.doc["security"]["key"]

    # ------------------------- Web 鉴权 -------------------------
    def get_auth(self) -> dict:
        a = self.doc.get("auth", {})
        return {
            "enabled": bool(a.get("enabled", True)),
            "username": a.get("username", DEFAULT_ADMIN_USER),
            "password_hash": a.get("password", ""),
        }

    def verify_auth(self, username: str, password: str) -> bool:
        a = self.get_auth()
        if not a["enabled"]:
            return False
        if username != a["username"]:
            return False
        return verify_password(password, a["password_hash"])

    def set_auth(self, username: str, password: str = None, enabled: bool = None):
        if "auth" not in self.doc:
            self.doc["auth"] = self._default_auth_table()
        a = self.doc["auth"]
        if username is not None:
            a["username"] = username
        if password is not None:
            a["password"] = hash_password(password)
        if enabled is not None:
            a["enabled"] = bool(enabled)
        self.save()

    # ------------------------- Steam 账号 -------------------------
    def get_steam_account(self) -> dict:
        s = self.doc.get("steam_account", {})
        enc = s.get("password", "") or ""
        return {
            "enabled": bool(s.get("enabled", False)),
            "username": s.get("username", ""),
            "password": decrypt_secret(enc, self.fernet_key()),
            "guard_code": s.get("guard_code", ""),
        }

    def set_steam_account(
        self,
        enabled: bool = None,
        username: str = None,
        password: str = None,
        guard_code: str = None,
    ):
        if "steam_account" not in self.doc:
            self.doc["steam_account"] = self._default_steam_table()
        s = self.doc["steam_account"]
        if enabled is not None:
            s["enabled"] = bool(enabled)
        if username is not None:
            s["username"] = username
        if password is not None:
            s["password"] = encrypt_secret(password, self.fernet_key())
        if guard_code is not None:
            s["guard_code"] = guard_code
        self.save()

    # ------------------------- 代理 -------------------------
    # ------------------------- 日志颜色策略 -------------------------
    def get_log_color(self) -> str:
        """返回日志颜色模式：auto / always / never。"""
        v = str((self.doc.get("logging") or {}).get("color", "auto")).lower()
        return v if v in ("auto", "always", "never") else "auto"

    def set_log_color(self, color: str):
        """设置日志颜色模式（auto/always/never），非法值抛 ConfigError。"""
        v = str(color or "auto").lower()
        if v not in ("auto", "always", "never"):
            raise ConfigError("logging.color 只能是 auto / always / never")
        if "logging" not in self.doc:
            self.doc["logging"] = self._default_logging_table()
        self.doc["logging"]["color"] = v
        self.save()

    def get_proxy(self) -> dict:
        p = self.doc.get("proxy", {})
        return {
            "enabled": bool(p.get("enabled", False)),
            "type": p.get("type", "socks5"),
            "host": p.get("host", "127.0.0.1"),
            "port": int(p.get("port", 1080)),
            "username": p.get("username", ""),
            "password": p.get("password", ""),
        }

    def set_proxy(
        self,
        enabled: bool = None,
        type: str = None,
        host: str = None,
        port: int = None,
        username: str = None,
        password: str = None,
    ):
        if "proxy" not in self.doc:
            self.doc["proxy"] = self._default_proxy_table()
        p = self.doc["proxy"]
        if enabled is not None:
            p["enabled"] = bool(enabled)
        if type is not None:
            p["type"] = type
        if host is not None:
            p["host"] = host
        if port is not None:
            p["port"] = int(port)
        if username is not None:
            p["username"] = username
        if password is not None:
            p["password"] = password
        self.save()

    # ------------------------- 自动更新 -------------------------
    # 取值范围：延时 0.1~10080 分钟；扫描间隔 0.01~720 小时
    DELAY_MINUTES_RANGE = (0.1, 10080)
    SCAN_MINUTES_RANGE = (1, 10080)

    def get_auto_update(self) -> dict:
        a = self.doc.get("auto_update", {})
        return {
            "enabled": bool(a.get("enabled", False)),
            "delay_enabled": bool(a.get("delay_enabled", True)),
            "delay_minutes": float(a.get("delay_minutes", 5)),
            "scan_enabled": bool(a.get("scan_enabled", True)),
            "scan_interval": float(a.get("scan_interval", 5)),
        }

    def set_auto_update(
        self,
        enabled: bool = None,
        delay_enabled: bool = None,
        delay_minutes: float = None,
        scan_enabled: bool = None,
        scan_interval: float = None,
    ):
        if "auto_update" not in self.doc:
            self.doc["auto_update"] = self._default_auto_update_table()
        a = self.doc["auto_update"]
        if enabled is not None:
            a["enabled"] = bool(enabled)
        if delay_enabled is not None:
            a["delay_enabled"] = bool(delay_enabled)
        if delay_minutes is not None:
            lo, hi = self.DELAY_MINUTES_RANGE
            v = float(delay_minutes)
            if not (lo <= v <= hi):
                raise ConfigError(f"延时时间需在 {lo}~{hi} 分钟之间")
            a["delay_minutes"] = v
        if scan_enabled is not None:
            a["scan_enabled"] = bool(scan_enabled)
        if scan_interval is not None:
            lo, hi = self.SCAN_MINUTES_RANGE
            v = float(scan_interval)
            if not (lo <= v <= hi):
                raise ConfigError(f"扫描间隔需在 {lo}~{hi} 分钟之间")
            a["scan_interval"] = v
        self.save()

    # ------------------------- 设置 -------------------------
    def get_settings(self) -> dict:
        s = self.doc.get("settings", {})
        return {
            "steamcmd_path": s.get("steamcmd_path", ""),
            "storage_dir": s.get("storage_dir", "./mods"),
            "host": s.get("host", "0.0.0.0"),
            "port": int(s.get("port", 8080)),
            "enable_broadcast": bool(s.get("enable_broadcast", True)),
            "broadcast_port": int(s.get("broadcast_port", 37021)),
            "broadcast_interval": int(s.get("broadcast_interval", 5)),
            "proxy": self.get_proxy(),
            "auto_update": self.get_auto_update(),
        }

    def update_settings(self, **kwargs):
        # 嵌套配置单独处理（proxy / auto_update 为字典）
        proxy_cfg = kwargs.pop("proxy", None)
        if isinstance(proxy_cfg, dict):
            self.set_proxy(**proxy_cfg)
        auto_cfg = kwargs.pop("auto_update", None)
        if isinstance(auto_cfg, dict):
            self.set_auto_update(**auto_cfg)

        if "settings" not in self.doc:
            self.doc["settings"] = tomlkit.table()
        s = self.doc["settings"]
        allowed = {
            "steamcmd_path", "storage_dir", "host", "port",
            "enable_broadcast", "broadcast_port", "broadcast_interval",
        }
        for k, v in kwargs.items():
            if k not in allowed:
                continue
            if k in ("port", "broadcast_port", "broadcast_interval"):
                v = int(v)
            elif k == "enable_broadcast":
                v = bool(v)
            s[k] = v
        self.save()

    # ------------------------- 游戏（独立 TOML） -------------------------
    def _games_dir(self) -> Path:
        d = self.path.parent / "games"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _game_path(self, appid: str) -> Path:
        return self._games_dir() / f"{appid}.toml"

    def _load_game_doc(self, appid: str):
        p = self._game_path(appid)
        if p.exists():
            try:
                with open(p, "r", encoding="utf-8") as f:
                    return tomlkit.load(f)
            except Exception:  # noqa: BLE001
                pass
        doc = tomlkit.document()
        doc["appid"] = str(appid)
        doc["name"] = ""
        doc["description"] = ""
        doc["mods"] = tomlkit.aot()
        return doc

    def _save_game_doc(self, appid: str, doc):
        p = self._game_path(appid)
        with open(p, "w", encoding="utf-8") as f:
            tomlkit.dump(doc, f)

    def get_games(self) -> list:
        games = self.doc.get("games", [])
        result = []
        for g in games:
            appid = str(g.get("appid", ""))
            gdoc = self._load_game_doc(appid)
            game_install = self._parse_install_table(gdoc.get("install"))
            mods = []
            for m in gdoc.get("mods", []):
                override = m.get("install_type")
                if override:
                    install = {"type": str(override),
                               "params": dict(m.get("install_params") or {})}
                else:
                    install = game_install
                mods.append({
                    "itemid": str(m.get("itemid", "")),
                    "name": m.get("name", ""),
                    "subscribed_url": m.get("subscribed_url", ""),
                    "source": m.get("source", "manual"),
                    "deps": list(m.get("deps", []) or []),
                    "install": install,
                })
            result.append({
                "appid": appid,
                "name": g.get("name", "") or gdoc.get("name", ""),
                "description": g.get("description", "") or gdoc.get("description", ""),
                "config_file": g.get("config_file", f"games/{appid}.toml"),
                "install": game_install,
                "mods": mods,
            })
        return result

    def get_game(self, appid: str):
        for g in self.doc.get("games", []):
            if str(g.get("appid", "")) == str(appid):
                return g
        return None

    def add_game(self, appid: str, name: str = "", description: str = ""):
        appid = self.normalize_game_input(appid)
        if self.get_game(appid):
            raise ConfigError(f"游戏 AppID {appid} 已存在")
        # 创建独立游戏配置文件
        gdoc = self._load_game_doc(appid)
        gdoc["appid"] = str(appid)
        gdoc["name"] = name
        gdoc["description"] = description
        if "mods" not in gdoc or not isinstance(gdoc.get("mods"), list):
            gdoc["mods"] = tomlkit.aot()
        self._save_game_doc(appid, gdoc)
        # 在主配置中登记
        entry = tomlkit.table()
        entry["appid"] = str(appid)
        entry["name"] = name
        entry["description"] = description
        entry["config_file"] = f"games/{appid}.toml"
        if "games" not in self.doc or not isinstance(self.doc["games"], list):
            self.doc["games"] = tomlkit.aot()
        self.doc["games"].append(entry)
        self.save()

    def update_game(self, appid: str, name: str = None, description: str = None):
        entry = self.get_game(appid)
        if not entry:
            raise ConfigError(f"游戏不存在: {appid}")
        if name is not None:
            entry["name"] = name
        if description is not None:
            entry["description"] = description
        gdoc = self._load_game_doc(appid)
        if name is not None:
            gdoc["name"] = name
        if description is not None:
            gdoc["description"] = description
        self._save_game_doc(appid, gdoc)
        self.save()

    # ------------------------- 安装方式 -------------------------
    @staticmethod
    def _parse_install_table(t) -> dict:
        """[install] 表 -> {"type": ..., "params": {其余键}}。"""
        t = t or {}
        itype = str(t.get("type", DEFAULT_INSTALL_TYPE))
        params = {k: v for k, v in t.items() if k != "type"}
        return {"type": itype, "params": params}

    def get_game_install(self, appid: str) -> dict:
        """游戏级默认安装方式。"""
        gdoc = self._load_game_doc(appid)
        return self._parse_install_table(gdoc.get("install"))

    def set_game_install(self, appid: str, itype: str, params: dict = None):
        """设置游戏级默认安装方式（写入 games/<appid>.toml 的 [install]）。"""
        gdoc = self._load_game_doc(appid)
        t = tomlkit.table()
        t["type"] = str(itype)
        for k, v in (params or {}).items():
            t[k] = v
        gdoc["install"] = t
        self._save_game_doc(appid, gdoc)

    def get_mod_install(self, appid: str, modid: str) -> dict:
        """某 Mod 的有效安装方式（游戏默认 + mod 级覆盖）。供下载解析用。"""
        gdoc = self._load_game_doc(appid)
        game_install = self._parse_install_table(gdoc.get("install"))
        for m in gdoc.get("mods", []):
            if str(m.get("itemid", "")) == str(modid):
                override = m.get("install_type")
                if override:
                    return {"type": str(override),
                            "params": dict(m.get("install_params") or {})}
                return game_install
        return game_install

    def delete_game(self, appid: str):
        entry = self.get_game(appid)
        if not entry:
            raise ConfigError(f"游戏不存在: {appid}")
        aot = self.doc["games"]
        for i, g in enumerate(aot):
            if str(g.get("appid", "")) == str(appid):
                del aot[i]
                break
        self.save()
        # 删除独立配置文件
        p = self._game_path(appid)
        if p.exists():
            try:
                p.unlink()
            except Exception:  # noqa: BLE001
                pass

    # ------------------------- Mod -------------------------
    @staticmethod
    def normalize_mod_input(text: str):
        """将用户输入（纯 ID 或创意工坊链接）解析为 (itemid, subscribed_url)。"""
        try:
            return parse_mod_url(text), ("" if re.fullmatch(r"\d+", (text or "").strip())
                                         else (text or "").strip())
        except Exception as e:  # noqa: BLE001
            raise ConfigError(str(e)) from e

    @staticmethod
    def normalize_game_input(text: str):
        """将用户输入（纯 AppID 或商店页地址）解析为 AppID。"""
        try:
            return parse_game_url(text)
        except Exception as e:  # noqa: BLE001
            raise ConfigError(str(e)) from e

    def add_mod(self, appid: str, mod_input: str, name: str = "", resolve_deps: bool = True):
        """添加 Mod（用户手动添加）。

        自动解析其 Steam 创意工坊依赖并一并添加（级联）。返回的字典包含：
          - added_main:        是否新增了主体 Mod
          - promoted:          若主体 Mod 此前为 auto（自动引入），本次手动添加后提升为 manual
          - added_dependencies: 本次自动引入的依赖 Mod 列表 [{itemid, name}]
          - skipped_existing:   已存在（未被重复添加）的依赖 itemid 列表
          - failed_dependencies: 依赖解析失败的输入 [{itemid, reason}]
          - dependency_warnings:  依赖**未能确认**（不是确定为无依赖）的输入 [{itemid, reason}]

        两者都带 reason：网络失败/超时/页面被 Steam 拦截/页面无依赖区块等，
        由上层直接展示给用户——不允许在无依赖信息的情况下静默通过。

        依赖以 source="auto" 写入；已存在的 Mod 不会重复添加。
        若把已存在的 auto Mod 手动添加，则提升为 manual（不再被自动清理）。
        """
        if not self.get_game(appid):
            raise ConfigError(f"游戏不存在: {appid}")
        itemid, subscribed_url = self.normalize_mod_input(mod_input)
        gdoc = self._load_game_doc(appid)
        if "mods" not in gdoc or not isinstance(gdoc.get("mods"), list):
            gdoc["mods"] = tomlkit.aot()

        result = {
            "itemid": itemid,
            "added_main": False,
            "promoted": False,
            "added_dependencies": [],
            "skipped_existing": [],
            "failed_dependencies": [],
            "dependency_warnings": [],
        }

        existing = self._find_mod(gdoc, itemid)
        if existing is not None:
            if existing.get("source", "manual") == "auto":
                # 用户显式手动添加：提升为 manual，避免其被自动清理
                existing["source"] = "manual"
                if name:
                    existing["name"] = name
                result["promoted"] = True
                result["added_main"] = True
            else:
                raise ConfigError(f"Mod {itemid} 已存在于该游戏")
        else:
            m = tomlkit.table()
            m["itemid"] = str(itemid)
            m["name"] = name
            m["subscribed_url"] = subscribed_url
            m["source"] = "manual"
            m["deps"] = []
            gdoc["mods"].append(m)
            result["added_main"] = True

        # 解析并级联添加依赖（主体 Mod 自身也会在此补全 deps 与名称）
        if resolve_deps and result["added_main"]:
            self._resolve_deps(gdoc, itemid, visited=set(), depth=0, result=result)

        # 主体名称仍未取到（用户留空且 Steam 拉取失败）时必须标记出来：
        # 界面据此明确告知"未自动获取到名称"，而不是留一个空名字让用户困惑
        main_mod = self._find_mod(gdoc, itemid)
        result["name_missing"] = bool(
            main_mod is not None and not (main_mod.get("name") or "").strip()
        )

        self._save_game_doc(appid, gdoc)
        return result

    def _resolve_deps(self, gdoc, itemid, visited, depth, result):
        """递归解析并添加依赖，补全每个节点的 deps 字段。

        itemid 已存在于 gdoc（主体或已存在的依赖）时，仅补全其 deps
        与缺失名称；不存在时以 source="auto" 新增，并继续递归其依赖。
        """
        key = str(itemid)
        if key in visited:
            return
        visited.add(key)
        if depth >= _MAX_DEP_DEPTH:
            return
        try:
            det = fetch_mod_details(key)
            name = det.get("name") or ""
            deps = det.get("dependencies") or []
        except SteamMetaError as e:
            # 带上具体原因（网络失败/超时/页面被拦截…），供界面明确告知用户
            result["failed_dependencies"].append(
                {"itemid": key, "reason": _brief_reason(e)})
            return
        if det.get("warning"):
            result["dependency_warnings"].append(
                {"itemid": key, "reason": det["warning"]})

        existing = self._find_mod(gdoc, key)
        if existing is None:
            m = tomlkit.table()
            m["itemid"] = key
            m["name"] = name
            m["subscribed_url"] = ""
            m["source"] = "auto"
            m["deps"] = deps
            gdoc["mods"].append(m)
            result["added_dependencies"].append({"itemid": key, "name": name})
        else:
            # 已存在：保留，但补全 deps 字段以保持依赖图完整
            existing["deps"] = deps
            if not existing.get("name"):
                existing["name"] = name
            result["skipped_existing"].append(key)

        for di in deps:
            self._resolve_deps(gdoc, di, visited, depth + 1, result)

    @staticmethod
    def _find_mod(gdoc, itemid):
        itemid = str(itemid)
        for m in gdoc.get("mods", []):
            if str(m.get("itemid", "")) == itemid:
                return m
        return None

    def delete_mod(self, appid: str, itemid: str):
        """删除指定 Mod，并级联清理「不再被任何手动 Mod 依赖」的自动依赖。

        返回 {"removed": [itemid], "removed_auto": [自动清理的依赖 itemid...]}。
        手动添加（source=manual）的 Mod 永远不会被自动删除；
        自动引入（source=auto）的依赖，仅在其不再被任何 manual Mod 可达依赖时删除。
        """
        if not self.get_game(appid):
            raise ConfigError(f"游戏不存在: {appid}")
        gdoc = self._load_game_doc(appid)
        mods = gdoc.get("mods")
        if not mods:
            raise ConfigError(f"Mod 不存在: {itemid}")
        target = str(itemid)
        idx = None
        for i, m in enumerate(mods):
            if str(m.get("itemid", "")) == target:
                idx = i
                break
        if idx is None:
            raise ConfigError(f"Mod 不存在: {itemid}")
        del mods[idx]
        # 级联清理孤儿自动依赖
        removed_auto = self._sweep_orphan_autos(gdoc)
        self._save_game_doc(appid, gdoc)
        return {"removed": [target], "removed_auto": removed_auto}

    def _sweep_orphan_autos(self, gdoc):
        """删除未被任何 manual Mod 可达依赖的 auto Mod。

        可达性 BFS：从全部 manual Mod 出发，沿 deps 边遍历；
        所有可达节点视为「被需要」。其余 auto Mod 视为孤儿并删除。
        """
        mods = gdoc.get("mods", [])
        depmap = {
            str(m.get("itemid", "")): set(str(d) for d in (m.get("deps") or []))
            for m in mods
        }
        needed = set()
        stack = [
            str(m.get("itemid", ""))
            for m in mods
            if m.get("source", "manual") == "manual"
        ]
        while stack:
            cur = stack.pop()
            if cur in needed:
                continue
            needed.add(cur)
            for d in depmap.get(cur, ()):
                if d not in needed:
                    stack.append(d)
        removed = []
        new_mods = tomlkit.aot()
        for m in mods:
            iid = str(m.get("itemid", ""))
            if m.get("source", "manual") == "auto" and iid not in needed:
                removed.append(iid)
            else:
                new_mods.append(m)
        gdoc["mods"] = new_mods
        return removed
