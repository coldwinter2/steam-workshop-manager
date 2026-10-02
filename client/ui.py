#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
客户端 UI（tkinter）

功能：
  - 服务端地址：优先使用本地保存的发现地址（P0）；仅当本地没有任何保存地址时
    才使用硬编码默认地址（P1）；两者都不适用/不可达时才临时启用广播发现（P2），
    发现后保存并停用广播监听。启动即自动连接，无需手工填写或点击连接
  - 设置：本地安装路径（可浏览选择）
  - Mod 列表：**树形展示**——手动添加的 Mod 为顶层节点，其自动依赖作为子节点
    嵌套显示（默认折叠，自动节点不可单独选中/下载/删除）；列表整体暗色主题
  - 选中：脱离复选框，改用行高亮——单击单选（取消其余），按住 Ctrl 多选
  - 右键菜单：弹出「常规下载 / 压缩下载 / 删除」，仅对鼠标所在条目生效
    （不支持批量）；自动依赖节点菜单禁用
    * 常规下载：请求 identity，服务端直传原始文件，不做压缩（默认最快）
    * 压缩下载：仅在用户主动选择时请求 gzip 压缩传输
    * 工具栏「下载所选 / 全部下载」一律走常规下载，避免服务端默认压缩
  - 下载速度：列表「速度」列与底部进度详情行按固定间隔（SPEED_INTERVAL_MS）
    实时刷新速率；压缩下载同时展示**等效速率（解压后，主显示）**与
    **网络速率（压缩传输）**，二者明确区分标注
    （速率计算与状态机见 speed.py，文案见 i18n.py）
  - 下载完成后自动刷新列表并清空选中

线程模型：耗时网络/IO 放后台线程，UI 更新统一走 root.after。
"""

import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from . import api, config, deps, endpoint, i18n, speed, store
from .config import (APPID, BROADCAST_PORT, BROADCAST_TIMEOUT,
                     DEFAULT_SERVER_URL, RECHECK_INTERVAL)

# ---- 下载速度显示 ----
SPEED_INTERVAL_MS = 500     # 速率刷新间隔（固定）
SPEED_STALL_AFTER = 2.0     # 超过该秒数无新数据 → 判定为「暂停」
SPEED_SMOOTHING = 0.4       # 速率平滑系数
DOWNLOAD_MAX_ATTEMPTS = 2   # 单个 Mod 最多尝试次数（含首次）
RETRY_DELAY = 1.5           # 重试前的等待（秒）

# ---- 配色（暗色主题） ----
BG = "#14141a"
BG2 = "#1c1c26"
BG3 = "#242430"
FG = "#e8e8f0"
FG_DIM = "#9090a0"
FG_FAINT = "#6f6f80"
ACCENT = "#4f8cff"
OK = "#3ecf8e"
WARN = "#f5a623"
ERR = "#ff5f56"
SEL_BG = "#2f4a7a"      # 选中行高亮背景
AUTO_FG = "#8f8fa6"     # 自动依赖节点文字色


class ClientApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title(f"Mod 同步客户端 · AppID {APPID}")
        root.geometry("980x700")
        root.minsize(820, 580)
        root.configure(bg=BG)

        rt = config.load_runtime_config()
        self.server_url = ""      # 由 EndpointManager 按优先级决定（保存地址优先）
        self.saved_url = rt.get("last_server_url", "")  # 本地保存的发现地址（优先）
        self.local_path = rt["local_path"]
        self.ep: "endpoint.EndpointManager" = None

        # 运行期数据
        self.manifest: dict = {}      # 分组清单（modid -> {元数据,安装,文件}）
        self.mods: list = []          # mod 字典列表（manifest.mods.values()）
        self.mods_by_id: dict = {}    # modid -> mod 字典
        self.graph: dict = {}         # itemid -> 直接依赖
        self.install_labels: dict = {}  # 安装类型 key -> 中文名
        self.downloaded: set = set()  # 本地已安装 itemid（来自状态）
        self.checked: set = set()     # 选中的「手动」Mod itemid（行高亮）
        self.row_map: dict = {}       # Treeview 行 iid -> {itemid, auto, mod}
        self.connected = False
        self.busy = False             # 下载/删除进行中
        self.game_name = ""

        # 下载速度显示（采样器 + 当前任务展示状态 + 定时器开关）
        self.meter = speed.SpeedMeter(interval=SPEED_INTERVAL_MS / 1000.0,
                                      stall_after=SPEED_STALL_AFTER,
                                      smoothing=SPEED_SMOOTHING)
        self.dl_state: dict = {}      # {idx,total,iid,path,done,total_bytes,compressed}
        self._speed_running = False   # 定时刷新是否进行中

        self._setup_style()
        self._build_ui()
        self._log(f"目标游戏 AppID = {APPID}（硬编码）")
        if self.saved_url:
            self._log(f"已保存服务端地址（优先使用）: {self.saved_url}")
        else:
            self._log(f"暂无保存地址，将使用默认地址: {DEFAULT_SERVER_URL}")
        self._log("启动后自动连接；优先已保存地址，无保存地址时用默认地址，最后才启用广播发现")
        # 启动即自动建立连接（不阻塞 UI 构建）
        self.root.after(200, self._start_endpoint)

    # ---------------- 主题 ----------------
    def _setup_style(self):
        """ttk 暗色主题（Treeview / 滚动条 / 进度条）。"""
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except Exception:  # noqa: BLE001
            pass
        style.configure(
            "Treeview",
            background=BG2, fieldbackground=BG2, foreground=FG,
            bordercolor=BG3, lightcolor=BG3, darkcolor=BG3,
            rowheight=26, borderwidth=0, font=("Microsoft YaHei UI", 9),
        )
        style.map("Treeview",
                  background=[("selected", SEL_BG)],
                  foreground=[("selected", FG)])
        style.configure(
            "Treeview.Heading",
            background=BG3, foreground=FG_DIM, relief="flat",
            bordercolor=BG3, lightcolor=BG3, darkcolor=BG3,
            font=("Microsoft YaHei UI", 9),
        )
        style.map("Treeview.Heading",
                  background=[("active", BG3), ("pressed", BG3)],
                  foreground=[("active", FG)])
        style.configure("Vertical.TScrollbar",
                        background=BG3, troughcolor=BG, bordercolor=BG,
                        arrowcolor=FG_DIM, relief="flat")
        style.map("Vertical.TScrollbar", background=[("active", ACCENT)])
        style.configure("Horizontal.TProgressbar",
                        background=ACCENT, troughcolor=BG3, bordercolor=BG3,
                        lightcolor=ACCENT, darkcolor=ACCENT)

    # ---------------- UI ----------------
    def _build_ui(self):
        # 顶部设置
        top = tk.Frame(self.root, bg=BG)
        top.pack(fill=tk.X, padx=12, pady=(10, 6))

        tk.Label(top, text="服务端", bg=BG, fg=FG_DIM).grid(row=0, column=0, sticky="w")
        # 服务端地址只读展示：优先本地保存的发现地址，无保存时才用默认（由 EndpointManager 维护）
        if self.saved_url:
            _ep_init = f"{self.saved_url} · 已保存地址（未连接）"
        else:
            _ep_init = f"{DEFAULT_SERVER_URL} · 默认地址（未连接）"
        self.ep_var = tk.StringVar(value=_ep_init)
        tk.Label(top, textvariable=self.ep_var, bg=BG, fg=FG, anchor="w",
                 width=34).grid(row=0, column=1, padx=(6, 8))

        tk.Label(top, text="本地路径", bg=BG, fg=FG_DIM).grid(row=0, column=2, sticky="w")
        self.path_var = tk.StringVar(value=self.local_path)
        tk.Entry(top, textvariable=self.path_var, width=34, bg=BG2, fg=FG,
                 insertbackground=FG, relief="flat").grid(row=0, column=3, padx=(6, 4))
        tk.Button(top, text="浏览…", command=self._browse_path, bg=BG3, fg=FG,
                  relief="flat", activebackground=ACCENT).grid(row=0, column=4, padx=(0, 8))

        self.save_btn = tk.Button(top, text="保存路径", command=self._save_settings,
                                  bg=BG3, fg=FG, relief="flat", activebackground=ACCENT)
        self.save_btn.grid(row=0, column=5, padx=(0, 4))
        # 自动连接失败时的手工补救入口（正常情况下无需点击）
        self.reconnect_btn = tk.Button(top, text="⟳ 重连", command=self._reconnect_now,
                                       bg=BG3, fg=FG, relief="flat",
                                       activebackground=ACCENT)
        self.reconnect_btn.grid(row=0, column=6)

        # 状态行
        status = tk.Frame(self.root, bg=BG)
        status.pack(fill=tk.X, padx=12, pady=(0, 6))
        self.status_var = tk.StringVar(value="未连接")
        tk.Label(status, text="状态:", bg=BG, fg=FG_DIM).pack(side=tk.LEFT)
        self.status_label = tk.Label(status, textvariable=self.status_var, bg=BG,
                                     fg=WARN, anchor="w")
        self.status_label.pack(side=tk.LEFT, padx=(4, 0))
        self.count_var = tk.StringVar(value="")
        tk.Label(status, textvariable=self.count_var, bg=BG, fg=FG_DIM).pack(side=tk.RIGHT)

        # 工具条
        tools = tk.Frame(self.root, bg=BG)
        tools.pack(fill=tk.X, padx=12, pady=(0, 4))
        for text, cmd in [
            ("全选", self._select_all), ("清空", self._clear_sel),
            ("反选", self._invert_sel),
            ("展开全部", lambda: self._expand_all(True)),
            ("折叠全部", lambda: self._expand_all(False)),
        ]:
            tk.Button(tools, text=text, command=cmd, bg=BG3, fg=FG,
                      relief="flat", activebackground=ACCENT).pack(side=tk.LEFT, padx=(0, 4))
        tk.Button(tools, text="🔄 刷新", command=self._refresh, bg=BG3, fg=FG,
                  relief="flat", activebackground=ACCENT).pack(side=tk.RIGHT)
        tk.Button(tools, text="🗑 删除所选", command=self._delete_selected,
                  bg="#5a2530", fg="white", relief="flat",
                  activebackground=ERR).pack(side=tk.RIGHT, padx=(0, 4))
        tk.Button(tools, text="⬇ 全部下载", command=self._download_all,
                  bg="#1f4a2e", fg="white", relief="flat",
                  activebackground=OK).pack(side=tk.RIGHT, padx=(0, 4))
        tk.Button(tools, text="⬇ 下载所选", command=self._download_selected,
                  bg=ACCENT, fg="white", relief="flat",
                  activebackground=ACCENT).pack(side=tk.RIGHT, padx=(0, 4))

        # 操作提示
        hint = tk.Frame(self.root, bg=BG)
        hint.pack(fill=tk.X, padx=12, pady=(0, 4))
        tk.Label(hint, text="提示：单击选中 / Ctrl 多选 · 右键单项操作（常规下载 / 压缩下载 / 删除）"
                            " · 手动 Mod 下的 ▶ 展开自动依赖",
                 bg=BG, fg=FG_DIM, anchor="w").pack(side=tk.LEFT)

        # Mod 列表（树形）
        mid = tk.Frame(self.root, bg=BG)
        mid.pack(fill=tk.BOTH, expand=True, padx=12, pady=(0, 6))

        cols = ("itemid", "source", "install", "deps", "size", "speed", "status")
        self.tree = ttk.Treeview(mid, columns=cols, show="tree headings",
                                 selectmode="none")
        self.tree.heading("#0", text="名称")
        self.tree.column("#0", width=250, anchor="w", stretch=True)
        headings = {
            "itemid": ("Mod ID", 110), "source": ("来源", 58),
            "install": ("安装", 74), "deps": ("依赖", 50),
            "size": ("大小", 80), "speed": ("速度", 168), "status": ("状态", 96),
        }
        for c, (h, w) in headings.items():
            self.tree.heading(c, text=h)
            self.tree.column(c, width=w, anchor="w" if c == "itemid" else "center")
        # 行标签（仅背景 / 仅前景，互不覆盖，避免优先级歧义）
        self.tree.tag_configure("sel", background=SEL_BG)
        self.tree.tag_configure("installed", foreground=OK)
        self.tree.tag_configure("unavailable", foreground=FG_FAINT)
        self.tree.tag_configure("auto", foreground=AUTO_FG)

        vsb = ttk.Scrollbar(mid, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        self.tree.bind("<Button-1>", self._on_tree_click)
        self.tree.bind("<Button-3>", self._on_tree_right_click)   # Windows / Linux
        self.tree.bind("<Button-2>", self._on_tree_right_click)   # macOS 备用

        # 进度
        prog = tk.Frame(self.root, bg=BG)
        prog.pack(fill=tk.X, padx=12, pady=(0, 4))
        self.prog_var = tk.DoubleVar(value=0)
        self.pbar = ttk.Progressbar(prog, variable=self.prog_var, maximum=100)
        self.pbar.pack(fill=tk.X)
        self.prog_label = tk.StringVar(value="")
        tk.Label(prog, textvariable=self.prog_label, bg=BG, fg=FG_DIM,
                 anchor="w").pack(fill=tk.X)

        # 日志
        logf = tk.Frame(self.root, bg=BG)
        logf.pack(fill=tk.BOTH, padx=12, pady=(0, 10))
        self.log = tk.Text(logf, height=7, bg=BG2, fg=FG, relief="flat",
                           insertbackground=FG, state="disabled")
        self.log.pack(fill=tk.BOTH, expand=True)

    # ---------------- 工具 ----------------
    def _log(self, msg: str):
        def _do():
            self.log.configure(state="normal")
            self.log.insert(tk.END, msg + "\n")
            self.log.see(tk.END)
            self.log.configure(state="disabled")
        self.root.after(0, _do)

    def _set_status(self, text, color=FG_DIM):
        self.root.after(0, lambda: (self.status_var.set(text),
                                    self.status_label.configure(fg=color)))

    def _browse_path(self):
        p = filedialog.askdirectory(title="选择 Mod 本地安装路径")
        if p:
            self.path_var.set(p)
            self._save_settings()

    def _save_settings(self):
        """仅持久化本地安装路径；服务端地址改为硬编码 + 广播动态获取。"""
        self.local_path = self.path_var.get().strip()
        config.save_runtime_config(self.local_path)
        self._log(f"本地安装路径已保存: {self.local_path or '（未设置）'}")
        self._log(f"配置文件: {config.config_path()}")
        if self.connected:
            self._scan_and_render()

    # ---------------- 连接 / 刷新 ----------------
    def _start_endpoint(self):
        """启动地址管理：自动连接 + 按需广播发现 + 自动重连。"""
        self.ep = endpoint.EndpointManager(
            default_url=DEFAULT_SERVER_URL,
            broadcast_port=BROADCAST_PORT,
            probe=self._probe,               # 轻量探活（/install/types）
            connect=self._do_connect,        # 真正建立连接（拉清单）
            on_lost=self._on_lost,
            on_broadcast=self._on_broadcast,
            on_discovering=self._on_discovering,
            load_saved=config.load_saved_url,
            save_discovered=config.save_last_server_url,
            is_busy=lambda: self.busy,       # 下载/删除中不切换服务端
            first_timeout=BROADCAST_TIMEOUT,
            recheck_interval=RECHECK_INTERVAL,
        )
        self._set_status("正在连接服务端…", WARN)
        self.ep.start()

    def _probe(self, url: str) -> bool:
        return api.ping(url)

    def _do_connect(self, url: str, source: str) -> bool:
        """按给定地址建立连接（后台线程执行），成功返回 True。"""
        try:
            self._load_from(url)
        except api.ApiError as e:
            self._log(f"连接 {url} 失败: {e}")
            return False
        self.server_url = url
        self._on_connected(url, source)
        return True

    def _on_broadcast(self, host: str, port: int):
        """收到服务端广播（仅记录/提示，是否连接由优先级链决定）。"""
        url = f"http://{host}:{port}"
        self._log(f"收到广播 {url}，尝试连接…")

    def _on_discovering(self):
        """进入广播发现阶段（当前固定地址不可达）。"""
        self._log("当前服务端地址不可达，正在通过局域网广播发现服务端…")
        self._set_status("正在通过广播发现服务端…", WARN)

    def _on_lost(self):
        self.connected = False
        self._set_status("服务端不可达，正在自动重连…", ERR)
        if self.ep and self.ep.saved_url:
            text = f"{self.ep.saved_url} · 已保存地址（未连接）"
        else:
            text = f"{DEFAULT_SERVER_URL} · 默认地址（未连接）"
        self.root.after(0, lambda: self.ep_var.set(text))
        self._log("与服务端断开，后台重试：优先已保存地址，无保存地址时用默认地址，最后广播发现")

    def _reconnect_now(self):
        """手工触发一次立即复核（自动连接失败时的补救）。"""
        if self.ep is None:
            self._start_endpoint()
            return
        self._set_status("正在重新连接…", WARN)
        self.ep.wake()

    def _load_from(self, url: str):
        """拉取分组清单（纯读）。失败抛 ApiError。"""
        manifest = api.get_mods(url, APPID)
        self.manifest = manifest
        self.game_name = manifest.get("name", "")
        self.mods_by_id = manifest.get("mods", {}) or {}
        self.mods = list(self.mods_by_id.values())
        self.graph = deps.build_graph(self.mods)
        self.install_labels = {
            t["key"]: t.get("label", t["key"])
            for t in (manifest.get("install_types") or [])
        }

    def _on_connected(self, url: str, source: str = ""):
        self.connected = True
        self.server_url = url
        src = source or (self.ep.source if self.ep else "")
        if src == endpoint.EndpointManager.SOURCE_DEFAULT:
            label = "默认地址"
        elif src == endpoint.EndpointManager.SOURCE_SAVED:
            self.saved_url = url          # 已保存地址（参与后续断开重连展示）
            label = "已保存地址"
        else:
            self.saved_url = url          # 广播发现的地址已被持久化，同步本地缓存
            label = "广播发现"
        self.root.after(0, lambda: self.ep_var.set(f"{url} · {label}"))
        self._set_status(f"已连接 · {label}", OK)
        if self.game_name:
            self._log(f"游戏: {self.game_name} (AppID {APPID})")
        self._log(f"已连接服务端 {url}（{label}），Mod 数: {len(self.mods)}")
        self._scan_and_render()

    def _refresh(self):
        if self.busy:
            return
        if not self.connected:
            self._reconnect_now()
            return
        self._set_status("刷新中…", WARN)
        threading.Thread(target=self._refresh_worker, daemon=True).start()

    def _refresh_worker(self):
        try:
            self._load_from(self.server_url)
        except api.ApiError as e:
            self._set_status(f"刷新失败: {e}", ERR)
            return
        self._scan_and_render()

    def _scan_and_render(self):
        self.downloaded = store.installed_mods(self.local_path) if self.local_path else set()
        self.root.after(0, self._render)

    # ---------------- 列表渲染（树形） ----------------
    def _manual_ids(self) -> set:
        return {str(m.get("itemid")) for m in self.mods
                if m.get("source") == "manual"}

    def _render(self):
        """重建树：手动 Mod 为根，自动依赖为子节点（默认折叠）。"""
        self.tree.delete(*self.tree.get_children())
        self.row_map = {}
        # 清理已不存在的选中（仅手动 Mod 可选中）
        self.checked &= self._manual_ids()

        placed: set = set()

        def insert(parent_row: str, row_iid: str, m: dict, is_auto: bool, stack: set):
            iid = str(m.get("itemid", ""))
            self._insert_row(parent_row, row_iid, m, is_auto)
            placed.add(iid)
            # 递归插入其依赖（作为自动子节点）
            for d in self.graph.get(iid, ()):
                d = str(d)
                if d in stack:
                    continue  # 防环
                dm = self.mods_by_id.get(d)
                if dm is None:
                    continue
                insert(row_iid, f"{row_iid}>{d}", dm, True, stack | {d})

        # 1) 手动 Mod 作为顶层根
        for m in self.mods:
            if m.get("source") != "manual":
                continue
            iid = str(m.get("itemid", ""))
            if not iid:
                continue
            insert("", iid, m, False, {iid})

        # 2) 兜底：未被展示的 Mod（如孤立 auto）也作为顶层显示，避免遗漏
        for m in self.mods:
            iid = str(m.get("itemid", ""))
            if not iid or iid in placed:
                continue
            insert("", iid, m, m.get("source") != "manual", {iid})

        self._update_count()

    def _row_tags(self, itemid: str, is_auto: bool, installed: bool, available: bool):
        tags = []
        if not is_auto and itemid in self.checked:
            tags.append("sel")
        if is_auto:
            tags.append("auto")
        elif not available:
            tags.append("unavailable")
        elif installed:
            tags.append("installed")
        return tuple(tags)

    def _insert_row(self, parent_row: str, row_iid: str, m: dict, is_auto: bool):
        iid = str(m.get("itemid", ""))
        name = m.get("name") or iid
        itype = (m.get("install") or {}).get("type", "copy")
        ilabel = self.install_labels.get(itype, itype)
        ndeps = len(m.get("deps") or [])
        available = m.get("available", bool(m.get("files")))
        installed = (iid in self.downloaded) and not is_auto
        size = store.mod_size(self.local_path, iid) if (self.local_path and iid in self.downloaded) else sum(f.get("size", 0) for f in m.get("files", {}))
        if not available:
            status = "服务端未下载"
        elif iid in self.downloaded:
            status = "已安装"
        else:
            status = "未安装"
        self.tree.insert(
            parent_row, tk.END, iid=row_iid, text=name,
            values=(iid, "自动" if is_auto else "手动", ilabel, ndeps,
                    self._fmt_size(size), "", status),
            tags=self._row_tags(iid, is_auto, installed, available),
        )
        self.row_map[row_iid] = {"itemid": iid, "auto": is_auto, "mod": m}

    def _fmt_size(self, n):
        for u in ("B", "KB", "MB", "GB"):
            if n < 1024:
                return f"{n:.1f} {u}"
            n /= 1024
        return f"{n:.1f} TB"

    def _update_count(self):
        manual_total = sum(1 for m in self.mods if m.get("source") == "manual")
        auto_total = len(self.mods) - manual_total
        self.count_var.set(
            f"手动 {manual_total} · 自动依赖 {auto_total} · "
            f"已下载 {len(self.downloaded)} · 已选 {len(self.checked)}"
        )

    def _apply_selection(self):
        """仅刷新各行的选中高亮标签（不重建树）。"""
        for row_iid, info in self.row_map.items():
            m = info["mod"]
            iid = info["itemid"]
            available = m.get("available", bool(m.get("files")))
            installed = (iid in self.downloaded) and not info["auto"]
            self.tree.item(row_iid,
                           tags=self._row_tags(iid, info["auto"], installed, available))
        self._update_count()

    def _expand_all(self, open_: bool):
        def rec(row: str):
            for c in self.tree.get_children(row):
                if self.tree.get_children(c):
                    self.tree.item(c, open=open_)
                rec(c)
        for r in self.tree.get_children(""):
            if self.tree.get_children(r):
                self.tree.item(r, open=open_)
            rec(r)

    # ---------------- 鼠标交互：选择 / 右键 ----------------
    def _select_row(self, row: str, ctrl: bool) -> bool:
        """选中某一行（仅手动 Mod 可选）。Ctrl=多选切换，否则单选取消其余。

        返回是否发生选中变化；自动依赖节点恒返回 False（不可单独选中）。
        """
        info = self.row_map.get(row)
        if not info or info["auto"]:
            return False
        iid = info["itemid"]
        if ctrl:
            if iid in self.checked:
                self.checked.discard(iid)
            else:
                self.checked.add(iid)
        else:
            self.checked = {iid}   # 单选：取消其余
        self._apply_selection()
        return True

    def _context_select(self, row: str):
        """右键命中的行为：仅选中鼠标所在条目（取消其余）。

        自动依赖不可单独操作 → 清空选中。返回该行 info（无则 None）。
        """
        info = self.row_map.get(row)
        if not info:
            return None
        if info["auto"]:
            if self.checked:
                self.checked = set()
                self._apply_selection()
        else:
            self.checked = {info["itemid"]}
            self._apply_selection()
        return info

    def _on_tree_click(self, event):
        """单击选中：Ctrl 多选切换；否则单选并取消其余；自动依赖不可选。

        展开逻辑：点击名称列（#0）且该行有子节点时手动切换展开/折叠，
        不依赖 identify_region 的 "indicator"（部分 Tk 8.6 对展开箭头
        返回 "tree"，导致原先无法展开）。
        """
        if self.busy:
            return "break"
        region = self.tree.identify_region(event.x, event.y)
        if region in ("heading", "separator"):
            return None            # 表头 / 分隔线交给默认行为
        row = self.tree.identify_row(event.y)
        if not row:
            # 点击空白处 -> 清空选中
            if self.checked:
                self.checked = set()
                self._apply_selection()
            return "break"
        # 名称列点击：有子节点则切换展开（与 Tk 版本无关，展开必生效）
        if self.tree.identify_column(event.x) == "#0" and self.tree.get_children(row):
            self.tree.item(row, open=not self.tree.item(row, "open"))
        self._select_row(row, bool(event.state & 0x0004))
        return "break"

    def _menu_states(self, itemid: str, available: bool):
        """按 Mod 下载状态计算右键菜单「下载 / 删除」按钮的可用状态。

        返回的下载状态同时作用于「常规下载」与「压缩下载」两个选项
        （二者只是传输方式不同，可用状态一致）。

        - 服务器未下载（available=False）→ 下载（两种）+ 删除都禁用
        - 本地已下载                    → 下载禁用（已有无需再下）、删除可用
        - 本地未下载（服务器有文件）      → 下载可用、删除禁用（本地无东西可删）
        """
        if not available:
            return "disabled", "disabled"
        if itemid in self.downloaded:
            return "disabled", "normal"
        return "normal", "disabled"

    def _on_tree_right_click(self, event):
        """右键：仅对鼠标所在条目生效；多选时先归并为该项。"""
        row = self.tree.identify_row(event.y)
        if not row:
            return "break"
        info = self._context_select(row)
        if not info:
            return "break"

        menu = tk.Menu(self.root, tearoff=0, bg=BG3, fg=FG,
                       activebackground=ACCENT, activeforeground="white",
                       bd=0, relief="flat")
        # 按下载状态决定各按钮可用性；任务进行中 / 未连接时统一禁用
        if info["auto"] or self.busy or not self.connected:
            dl_state = del_state = "disabled"
        else:
            available = info["mod"].get("available", bool(info["mod"].get("files")))
            dl_state, del_state = self._menu_states(info["itemid"], available)
        # 下载拆分为两个独立选项：互不影响，均按当前 Mod 下载状态控权
        menu.add_command(label="⬇ 常规下载（原始文件直传）",
                         command=lambda i=info["itemid"]: self._ctx_download(
                             i, compressed=False),
                         state=dl_state)
        menu.add_command(label="🗜 压缩下载（打包压缩传输）",
                         command=lambda i=info["itemid"]: self._ctx_download(
                             i, compressed=True),
                         state=dl_state)
        menu.add_command(label="🗑 删除该 Mod",
                         command=lambda i=info["itemid"]: self._ctx_delete(i),
                         state=del_state)
        if info["auto"]:
            menu.add_separator()
            menu.add_command(label="（自动依赖，随其手动 Mod 一并操作）", state="disabled")
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()
        return "break"

    def _ctx_download(self, itemid: str, compressed: bool = False):
        """右键下载：compressed 决定走压缩传输还是原始文件直传。"""
        if self.busy or not self.connected:
            return
        if not self.local_path:
            messagebox.showwarning("提示", "请先设置本地安装路径")
            return
        self._start_download([str(itemid)], compressed=compressed)

    def _ctx_delete(self, itemid: str):
        if self.busy or not self.connected:
            return
        self._delete_items([str(itemid)])

    # ---------------- 选择（工具栏） ----------------
    def _select_all(self):
        if self.busy:
            return
        self.checked = set(self._manual_ids())
        self._apply_selection()

    def _clear_sel(self):
        if self.busy:
            return
        self.checked = set()
        self._apply_selection()

    def _invert_sel(self):
        if self.busy:
            return
        self.checked = self._manual_ids() - self.checked
        self._apply_selection()

    # ---------------- 下载 ----------------
    # 两种下载方式（互不干扰，仅由用户选择决定）：
    #   常规下载 compressed=False —— 原始文件直传，服务端不压缩（默认）
    #   压缩下载 compressed=True  —— 请求服务端 gzip 压缩传输
    def _download_selected(self):
        """工具栏「下载所选」：走常规下载（不压缩）。"""
        if self._guard():
            return
        if not self.checked:
            messagebox.showinfo("提示", "请先选中要下载的 Mod")
            return
        self._start_download(sorted(self.checked), compressed=False)

    def _download_all(self):
        """工具栏「全部下载」：走常规下载（不压缩）。"""
        if self._guard():
            return
        ids = sorted(self._manual_ids())
        if not ids:
            messagebox.showinfo("提示", "服务端暂无 Mod")
            return
        self._start_download(ids, compressed=False)

    def _start_download(self, selected: list, compressed: bool = False):
        if not self.local_path:
            messagebox.showwarning("提示", "请先设置本地安装路径")
            return
        plan = deps.plan_download(self.graph, selected)
        if not plan:
            return
        self.busy = True
        mode = i18n.t("mode_compressed") if compressed else i18n.t("mode_normal")
        self._set_status(f"下载中 0/{len(plan)} · {mode}", WARN)
        self._log(f"[{mode}] 下载计划 {len(plan)} 个 Mod（含依赖）: {', '.join(plan)}")
        self.dl_state = {"compressed": compressed}
        self._speed_start()
        threading.Thread(target=self._download_worker,
                         args=(plan, compressed), daemon=True).start()

    # ---------------- 下载速度：定时刷新与文案 ----------------
    def _speed_start(self):
        """启动固定间隔的速率刷新（整个下载任务期间只启动一次）。"""
        self.meter.reset()
        self._speed_running = True
        self._speed_tick()

    def _speed_stop(self):
        """停止速率刷新并冻结当前速率（完成后不再跳动）。"""
        self._speed_running = False
        self.meter.finish()

    def _speed_tick(self):
        if not self._speed_running:
            return
        try:
            self._render_speed(self.meter.sample())
        except Exception:  # noqa: BLE001 - 刷新异常不应影响下载本身
            pass
        self.root.after(SPEED_INTERVAL_MS, self._speed_tick)

    def _render_speed(self, snap):
        """把一次采样结果渲染到「列表速度列」与「进度详情行」。"""
        st = self.dl_state
        iid = st.get("iid")
        if not iid:
            return
        self.prog_label.set(
            f"{iid} · {st.get('path', '')} {self._fmt_size(st.get('done', 0))}"
            + self._speed_detail_suffix(snap)
        )
        cell = self._speed_cell_text(snap)
        for row, info in self.row_map.items():
            if info["itemid"] == iid:
                self.tree.set(row, "speed", cell)

    def _speed_detail_suffix(self, snap) -> str:
        """进度详情行尾部的速度段（含等待 / 暂停 / 重试 / 完成等边界文案）。"""
        m = speed.SpeedMeter
        if snap.state == m.DONE:
            return " · " + i18n.t("speed_done")
        if snap.state == m.WAITING:
            return " · " + i18n.t("speed_waiting")
        if snap.state == m.STALLED:
            return " · " + i18n.t("speed_stalled")
        if snap.state == m.RETRY:
            return " · " + i18n.t("speed_retry", n=snap.attempt)
        if self.dl_state.get("compressed"):
            # 压缩下载：等效速率（解压后）为主，网络速率（压缩传输）明确标注
            return (" · " + i18n.t("speed_effective",
                                   rate=speed.fmt_rate(snap.effective))
                    + " · " + i18n.t("speed_network",
                                     rate=speed.fmt_rate(snap.wire))
                    + i18n.t("speed_note_compressed"))
        return " · " + i18n.t("speed_label", rate=speed.fmt_rate(snap.effective))

    def _speed_cell_text(self, snap) -> str:
        """列表「速度」列文本（压缩下载时同一行内并列展示两条速率）。"""
        m = speed.SpeedMeter
        if snap.state == m.DONE:
            return i18n.t("speed_done")
        if snap.state == m.WAITING:
            return i18n.t("speed_waiting")
        if snap.state == m.STALLED:
            return i18n.t("speed_stalled")
        if snap.state == m.RETRY:
            return i18n.t("speed_retry", n=snap.attempt)
        if self.dl_state.get("compressed"):
            return i18n.t("speed_cell_compressed",
                          eff=speed.fmt_rate(snap.effective),
                          wire=speed.fmt_rate(snap.wire))
        return speed.fmt_rate(snap.effective)

    def _clear_speed_cells(self):
        """清空列表中的速度列（任务结束 / 列表重建时）。"""
        try:
            for row in self.row_map:
                self.tree.set(row, "speed", "")
        except Exception:  # noqa: BLE001
            pass

    def _download_worker(self, plan: list, compressed: bool = False):
        """后台下载线程；无论正常结束还是异常，都停止速率刷新并复位界面。"""
        ok = skip = fail = 0
        try:
            ok, skip, fail = self._run_download_plan(plan, compressed)
        finally:
            self._speed_stop()          # 停止定时刷新（速度不再跳动）
            self._clear_speed_cells()
            self.root.after(0, lambda: self.prog_var.set(0))
            self.prog_label.set("")
            self.busy = False
            self._after_download_refresh()
        self._set_status(
            f"完成：安装 {ok}，跳过 {skip}，失败 {fail}",
            OK if fail == 0 else WARN,
        )
        self._log(f"下载安装结束：成功 {ok}，跳过 {skip}，失败 {fail}")

    def _run_download_plan(self, plan: list, compressed: bool = False):
        """按计划逐个下载安装；返回 (ok, skip, fail)。"""
        ok = skip = fail = 0
        total = len(plan)
        for idx, iid in enumerate(plan, 1):
            self._set_status(f"下载中 {idx}/{total} · Mod {iid}", WARN)
            meta = self.mods_by_id.get(iid)
            if meta is None:
                skip += 1
                self._log(f"[{idx}/{total}] Mod {iid} 不在清单，跳过")
                continue
            files = meta.get("files") or []
            if not files or not meta.get("available", True):
                skip += 1
                self._log(f"[{idx}/{total}] Mod {iid} 服务端暂无文件，跳过")
                continue
            base_total = sum(f.get("size", 0) for f in files)
            self.dl_state = {"idx": idx, "total": total, "iid": iid, "path": "",
                             "done": 0, "total_bytes": base_total,
                             "compressed": compressed}

            # 跨文件累计：done=解压后原始字节（等效），wire=网络接收字节
            acc = {"file": None, "f_done": 0, "f_wire": 0, "done": 0, "wire": 0}

            def cb(path, done, tot, _acc=acc, _bt=base_total):
                if path != _acc["file"]:            # 新文件：单文件计数归零
                    _acc["file"] = path
                    _acc["f_done"] = 0
                    _acc["f_wire"] = 0
                step = done - _acc["f_done"]
                if step > 0:
                    _acc["f_done"] = done
                    _acc["done"] += step
                if _bt:
                    pct = done / _bt * 100
                    self.root.after(0, lambda: self.prog_var.set(min(pct, 100)))
                self.meter.update(_acc["done"], _acc["wire"])
                self.dl_state["path"] = path
                self.dl_state["done"] = done

            def tcb(wire, _acc=acc):
                step = wire - _acc["f_wire"]
                if step > 0:
                    _acc["f_wire"] = wire
                    _acc["wire"] += step
                self.meter.update(_acc["done"], _acc["wire"])

            res = None
            attempt = 0
            while attempt < DOWNLOAD_MAX_ATTEMPTS:
                attempt += 1
                # 每次尝试都重新计数：失败前的字节不计入速率
                acc.update({"file": None, "f_done": 0, "f_wire": 0,
                            "done": 0, "wire": 0})
                if attempt == 1:
                    self.meter.reset()
                else:
                    self.meter.mark_retry(attempt)
                    self._log(f"[{idx}/{total}] ↻ Mod {iid} 失败重试（第 {attempt} 次）…")
                    time.sleep(RETRY_DELAY)
                try:
                    res = store.download_and_install(
                        self.server_url, self.local_path, APPID, meta,
                        progress_cb=cb, traffic_cb=tcb, compress=compressed,
                    )
                    break
                except store.StoreError as e:
                    self._log(f"[{idx}/{total}] ✗ Mod {iid} 第 {attempt} 次尝试失败: {e}")
                    if attempt >= DOWNLOAD_MAX_ATTEMPTS:
                        fail += 1
            if res is None:
                continue
            self.meter.finish()      # 冻结该 Mod 的速率，完成后不再跳动
            ok += 1
            self._log(
                f"[{idx}/{total}] ✓ Mod {iid} 下载 {self._fmt_size(res['bytes'])}"
                f" · 安装[{res['type']}] {res['message']}"
            )
        return ok, skip, fail

    def _after_download_refresh(self):
        """下载完成后的收尾：清空全部选中状态并刷新列表。"""
        self.checked = set()
        self._scan_and_render()

    # ---------------- 删除 ----------------
    def _delete_selected(self):
        if self._guard():
            return
        if not self.checked:
            messagebox.showinfo("提示", "请先选中要删除的 Mod")
            return
        self._delete_items(sorted(self.checked))

    def _delete_items(self, selected: list):
        """删除给定「手动」Mod 及其孤儿依赖（共享依赖保留）。"""
        if not self.local_path:
            messagebox.showwarning("提示", "请先设置本地安装路径")
            return
        manual = self._manual_ids()
        to_delete, kept = deps.plan_delete(self.graph, selected,
                                           self.downloaded, manual)
        # 仅保留本地实际存在的
        to_delete_local = sorted(to_delete & self.downloaded)
        kept_local = sorted(kept & self.downloaded)
        if not to_delete_local:
            messagebox.showinfo("提示", "所选 Mod 本地均未下载，无需删除")
            return
        msg = ["将从本地删除以下 Mod：", ""]
        msg += [f"  • {self._name_of(i)}" for i in to_delete_local]
        if kept_local:
            msg += ["", "保留的共享依赖（仍被其它已下载 Mod 需要）："]
            msg += [f"  • {self._name_of(i)}" for i in kept_local]
        msg += ["", "此操作只影响本地文件，不影响服务端。", "", "确认删除？"]
        if not messagebox.askyesno("确认删除", "\n".join(msg)):
            return
        self.busy = True
        threading.Thread(target=self._delete_worker, args=(to_delete_local,),
                         daemon=True).start()

    def _delete_worker(self, targets: list):
        removed = store.delete_mods(self.local_path, targets)
        self._log(f"本地删除 {len(removed)} 个 Mod: {', '.join(removed)}")
        if removed:
            for i in removed:
                self.checked.discard(i)
        self.busy = False
        self._scan_and_render()
        self._set_status(f"已删除 {len(removed)} 个 Mod（仅本地）", OK)

    def _name_of(self, iid):
        for m in self.mods:
            if str(m.get("itemid")) == iid:
                return f"{m.get('name') or iid} ({iid})"
        return iid

    def _guard(self) -> bool:
        if self.busy:
            messagebox.showinfo("提示", "当前有任务进行中，请稍候")
            return True
        if not self.connected:
            messagebox.showinfo("提示", "尚未连接服务端，客户端正在自动重连，请稍候")
            return True
        return False

    def run(self):
        self.root.mainloop()


def main():
    root = tk.Tk()
    ClientApp(root).run()


if __name__ == "__main__":
    main()
