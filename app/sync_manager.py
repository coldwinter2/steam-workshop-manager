#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
同步管理器

负责：
  - 触发并追踪一次同步任务（后台线程顺序下载所有已配置 Mod）
  - 扫描本地已下载 Mod，生成清单（文件、大小、修改时间）
"""

import re
import shutil
import threading
import time
import uuid
from pathlib import Path

from .config_manager import ConfigError, ConfigManager
from .steam_acf import remove_acf_file as acf_remove_file
from .steam_acf import remove_items as acf_remove_items
from .steamcmd_runner import SteamCMD, SteamCMDError


class SyncTask:
    """一次同步任务的运行状态。

    日志采用「带单调递增序号的条目」保存，而不是裸字符串列表，原因是前端
    刷新/断线后要能**从断点续传**：

      - 每条日志有唯一且递增的 `seq`，客户端记录已收到的最大 seq，下次带
        `since=<seq>` 请求，服务端只下发更大的那些 —— 天然保序、不重不漏；
      - steamcmd 的进度行会被折叠为一行并计数（`×N`），这种「改写上一行」的操作
        会生成**新 seq 但保持同一行 id**：客户端按 id 覆盖对应行即可，既不重复
        也不会留下过期行。注意不能用「列表下标」或「被替换行的 seq」做增量：
        服务端已丢掉中间版本，客户端拿着更早的 seq 会匹配不上，从而多出残留行；
      - 条目数超过 MAX_LOG_ENTRIES 会丢弃最旧的并记录 `drop_seq`，迟到的客户端
        据此知道自己拿到的是被截断的部分，而不是「日志只有这么点」。
    """

    # 单任务最多保留多少条日志（超出丢弃最旧的，防止长时间任务撑爆内存）
    MAX_LOG_ENTRIES = 5000

    def __init__(self, task_id: str, label: str = ""):
        self.task_id = task_id
        self.label = label or ""
        self.status = "running"  # running | finished
        self.error: str | None = None
        self.results: list[dict] = []
        self.started_at = time.time()
        self.finished_at = None
        self.last_log_at = time.time()
        self._entries: list[dict] = []
        self._seq = 0
        self._drop_seq = 0
        self._log_lock = threading.Lock()

    # ------------------------- 写日志 -------------------------
    def log(self, msg: str, replace_last: bool = False, ts: str = None):
        """写入一条日志。

        replace_last=True 时覆盖上一条：分配新 seq（保证>任何已下发游标，会被重新
        投递），但沿用同一个行 id（客户端据此原地覆盖，而不是再追加一行）。
        """
        with self._log_lock:
            self._seq += 1
            if replace_last and self._entries:
                old = self._entries[-1]
                entry = {"seq": self._seq, "id": old["id"],
                         "time": ts or time.strftime("%H:%M:%S"), "msg": msg}
                self._entries[-1] = entry
            else:
                entry = {"seq": self._seq, "id": self._seq,
                         "time": ts or time.strftime("%H:%M:%S"), "msg": msg}
                self._entries.append(entry)
            self.last_log_at = time.time()
            if len(self._entries) > self.MAX_LOG_ENTRIES:
                drop = len(self._entries) - self.MAX_LOG_ENTRIES
                self._entries = self._entries[drop:]
                self._drop_seq = self._entries[0]["seq"] - 1
            return entry

    # ------------------------- 读日志 -------------------------
    def logs_since(self, since: int = 0) -> tuple[list[dict], bool, int]:
        """返回 (seq 大于 since 的日志副本, 是否已被截断, 当前最大 seq)。"""
        with self._log_lock:
            if since < self._drop_seq:
                # 客户端要的游标已被丢弃 -> 只能给出当前保留的全部，并标记为截断
                entries = [dict(e) for e in self._entries]
                truncated = bool(entries)
            else:
                entries = [dict(e) for e in self._entries if e["seq"] > since]
                truncated = False
            return entries, truncated, self._seq

    @property
    def logs(self) -> list[str]:
        """兼容旧用法：纯文本日志列表。"""
        with self._log_lock:
            return [f"[{e['time']}] {e['msg']}" for e in self._entries]

    def last_seq(self) -> int:
        with self._log_lock:
            return self._seq

    def idle_seconds(self) -> float:
        return max(0.0, time.time() - self.last_log_at)

    # ------------------------- API 视图 -------------------------
    def view(self, since: int = 0) -> dict:
        """给前端的增量视图：只包含 since 之后的日志，附任务状态与汇总。"""
        entries, truncated, cursor = self.logs_since(since)
        results = list(self.results)
        ok = sum(1 for r in results if r.get("ok"))
        return {
            "task_id": self.task_id,
            "label": self.label,
            "status": self.status,
            "error": self.error,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "elapsed": (self.finished_at or time.time()) - self.started_at,
            "cursor": cursor,
            "truncated": truncated,
            "logs": entries,
            "results": results,
            "summary": {"ok": ok, "fail": len(results) - ok, "total": len(results)},
            "idle_seconds": round(self.idle_seconds()),
            "server_time": time.time(),
        }


class SyncManager:
    """同步任务调度与本地清单扫描。"""

    # 每批最多下载多少个 Mod：既保留「一次登录下载多个」的优势，
    # 又限制单批失败的影响面（配合下面的失败降级逐个重试）。
    BATCH_CHUNK = 50
    # 内存中最多保留多少个已完成任务的记录（防止长时间运行后无限增长）
    MAX_HISTORY = 20

    def __init__(self, config: ConfigManager):
        self.config = config
        self.tasks: dict[str, SyncTask] = {}
        self._lock = threading.Lock()
        self._current: SyncTask | None = None
        # 任务完成回调（任何来源的同步任务结束都会调用，参数为 SyncTask）
        self._finish_hooks: list = []

    def set_finish_hook(self, fn):
        """注册完成钩子（多次注册可叠加，异常不会向外抛出）。"""
        if callable(fn) and fn not in self._finish_hooks:
            self._finish_hooks.append(fn)

    # ------------------------- 路径 -------------------------
    def storage_abs(self) -> Path:
        return Path(self.config.get_settings()["storage_dir"]).resolve()

    def install_dir_for(self, appid: str) -> Path:
        return self.storage_abs() / str(appid)

    def content_dir_for(self, appid: str) -> Path:
        return self.install_dir_for(appid) / "steamapps" / "workshop" / "content" / str(appid)

    def item_dir(self, appid: str, itemid: str) -> Path:
        return self.content_dir_for(appid) / str(itemid)

    def acf_dir(self, appid: str) -> Path:
        """该游戏 ACF 所在目录：<storage>/<appid>/steamapps/workshop。"""
        return self.install_dir_for(appid) / "steamapps" / "workshop"

    def _acf_extra_dirs(self) -> list[Path]:
        """ACF 的备用查找目录。

        steamcmd 未配 `+force_install_dir` 时，`appworkshop_<appid>.acf` 会落在
        **steamcmd 安装目录**的 `steamapps/workshop/`，而不是我们的下载目录。
        这里把 steamcmd 所在目录也纳入查找范围，两种布局都能命中。
        """
        out: list[Path] = []
        try:
            raw = str(self.config.get_settings().get("steamcmd_path") or "").strip()
            if not raw:
                return out
            exe = SteamCMD(raw).resolve()
            out.append(exe.parent)      # steamcmd.exe / steamcmd.sh 所在目录
        except Exception:  # noqa: BLE001  路径未配置/异常时忽略备用位置
            pass
        return out

    def sync_acf_records(self, appid: str, itemids: list[str]) -> dict:
        """把若干 Mod 的下载记录从 ACF 中摘除。

        **必须在删除 Mod 文件之后调用**：文件没了但 ACF 里仍记着，steamcmd
        会认定该 Mod 已下载而跳过重下（删除后无法再取回文件）。详见 steam_acf 模块。

        返回 {"acf": 相对/绝对路径或 None, "removed": [...], "missing": [...], "error": ...}
        """
        try:
            appid = self._safe_id(appid, "appid")
        except ValueError as e:
            return {"acf": None, "removed": [], "missing": [], "error": str(e)}
        res = acf_remove_items(
            appid, itemids,
            install_dir=self.install_dir_for(appid),
            extra_dirs=self._acf_extra_dirs(),
        )
        path = res.get("path")
        rel = None
        if path:
            try:
                rel = str(Path(path).relative_to(self.storage_abs()))
            except ValueError:
                rel = path       # 落在 storage 之外（如 steamcmd 目录）就报绝对路径
        return {
            "acf": rel,
            "removed": res.get("removed") or [],
            "missing": res.get("missing") or [],
            "error": res.get("error"),
        }

    # ------------------------- 文件清理 -------------------------
    @staticmethod
    def _safe_id(value: str, what: str) -> str:
        """appid/itemid 必须是纯数字：它们要拼进删除路径，防路径穿越。"""
        s = str(value)
        if not s.isdigit():
            raise ValueError(f"非法 {what}: {value!r}")
        return s

    def delete_mod_files(self, appid: str, itemids: list[str]) -> dict:
        """删除若干 Mod 的已下载文件（目录 + extract 安装方式的 zip 缓存）。

        只清理落在 storage 下载布局内的固定子路径，路径均由纯数字 id 拼出；
        单个目标删除失败不中断，汇总到 errors 返回。
        返回 {"deleted": [相对路径...], "errors": [{"target", "error"}]}。
        """
        appid = self._safe_id(appid, "appid")
        deleted: list[str] = []
        errors: list[dict] = []
        seen: set[str] = set()
        for raw in itemids:
            try:
                itemid = self._safe_id(raw, "itemid")
            except ValueError as e:
                errors.append({"target": str(raw), "error": str(e)})
                continue
            if itemid in seen:
                continue
            seen.add(itemid)
            targets = [
                self.item_dir(appid, itemid),
                self.install_dir_for(appid) / ".mod_zips" / f"{itemid}.zip",
            ]
            for p in targets:
                if not p.exists():
                    continue
                try:
                    if p.is_dir():
                        shutil.rmtree(p)
                    else:
                        p.unlink()
                    deleted.append(str(p.relative_to(self.storage_abs())))
                except Exception as e:  # noqa: BLE001
                    errors.append({"target": str(p), "error": f"{type(e).__name__}: {e}"})
        return {"deleted": deleted, "errors": errors}

    def delete_game_files(self, appid: str) -> dict:
        """删除某游戏的整个下载目录 <storage>/<appid>（含 .mod_zips 等全部内容）。

        同时删除 `appworkshop_<appid>.acf`（若该 ACF 落在 steamcmd 自身目录而非
        storage 下，单删 storage 目录是删不到的）：文件删了记录还在，会让 steamcmd
        认为工坊条目仍然有效。
        """
        appid = self._safe_id(appid, "appid")
        root = self.install_dir_for(appid)
        deleted, errors = [], []
        if root.exists():
            try:
                shutil.rmtree(root)
                deleted.append(str(root.relative_to(self.storage_abs())))
            except Exception as e:  # noqa: BLE001
                errors.append({"target": str(root), "error": f"{type(e).__name__}: {e}"})
        # storage 下的 ACF 已随目录删除；这里只清理「落在其它位置」的那份
        acf = acf_remove_file(appid, extra_dirs=self._acf_extra_dirs())
        if acf.get("error"):
            errors.append({"target": acf.get("path") or "appworkshop",
                           "error": f"删除 ACF 失败: {acf['error']}"})
        elif acf.get("deleted"):
            try:
                rel = str(Path(acf["path"]).relative_to(self.storage_abs()))
            except ValueError:
                rel = acf["path"]
            deleted.append(rel)
        return {"deleted": deleted, "errors": errors}

    # ------------------------- 任务 -------------------------
    def current_task_id(self) -> str | None:
        return self._current.task_id if self._current else None

    def start_sync(self) -> str | None:
        """启动一次全量同步，返回任务 ID；若已有任务在跑则返回 None。"""
        games = self.config.get_games()
        targets: list[dict] = []
        for g in games:
            for m in g["mods"]:
                targets.append({
                    "appid": g["appid"], "itemid": m["itemid"],
                    "name": m.get("name") or "", "game_name": g.get("name") or "",
                })
        if not targets:
            raise ConfigError("尚未配置任何 Mod，无法同步")
        return self._start_task(targets, label="全量同步")

    def start_update(self, appid: str, itemids: list[str] = None) -> str | None:
        """更新指定游戏下的 Mod。

        itemids 为 None 时更新该游戏下全部 Mod；
        否则仅更新 itemids 中指定的 Mod。
        返回任务 ID；若已有任务在跑则返回 None。
        """
        games = {g["appid"]: g for g in self.config.get_games()}
        if appid not in games:
            raise ConfigError(f"游戏不存在: {appid}")
        g = games[appid]
        mods = g["mods"]
        if itemids is not None:
            want = {str(i) for i in itemids}
            mods = [m for m in mods if str(m["itemid"]) in want]
            if not mods:
                raise ConfigError(f"指定的 Mod 均不存在于游戏 {appid}")
        if not mods:
            raise ConfigError(f"游戏 {appid} 下没有 Mod")
        targets = [{
            "appid": appid, "itemid": m["itemid"],
            "name": m.get("name") or "", "game_name": g.get("name") or "",
        } for m in mods]
        return self._start_task(targets, label=f"更新 [{g.get('name') or appid}]")

    # ------------------------- 未下载 Mod（自动更新用） -------------------------
    def is_downloaded(self, appid: str, itemid: str) -> bool:
        """该 Mod 是否已下载（目录存在且非空）。"""
        d = self.item_dir(appid, itemid)
        try:
            return d.exists() and any(d.iterdir())
        except OSError:
            return False

    def pending_targets(self) -> list[dict]:
        """尚未下载的 Mod 列表（已下载的一律跳过，不做覆盖/重复下载）。"""
        targets: list[dict] = []
        for g in self.config.get_games():
            for m in g["mods"]:
                if self.is_downloaded(g["appid"], m["itemid"]):
                    continue
                targets.append({
                    "appid": g["appid"], "itemid": m["itemid"],
                    "name": m.get("name") or "", "game_name": g.get("name") or "",
                })
        return targets

    def start_pending(self, label: str = "自动更新（仅未下载）") -> tuple:
        """只更新尚未下载的 Mod。

        返回 (task_id, pending_count)：
          - task_id 为 None 且 count 为 0：没有需要下载的 Mod
          - task_id 为 None 且 count > 0：已有任务在运行（本次未启动）
        """
        targets = self.pending_targets()
        if not targets:
            return None, 0
        return self._start_task(targets, label=label), len(targets)

    def _start_task(self, targets: list[dict], label: str) -> str | None:
        """创建后台任务并启动线程。"""
        with self._lock:
            if self._current is not None and self._current.status == "running":
                return None
            task_id = uuid.uuid4().hex[:8]
            task = SyncTask(task_id, label=label)
            task.log(f"任务类型: {label}（共 {len(targets)} 个 Mod）")
            self.tasks[task_id] = task
            self._current = task
            self._trim_tasks()
        threading.Thread(target=self._execute, args=(task, targets), daemon=True).start()
        return task_id

    def _trim_tasks(self):
        """裁剪历史任务记录：只保留最近 MAX_HISTORY 个已完成任务，当前任务永不清。

        注意保留「最近完成的那个」，否则界面刷新取不到刚结束任务的日志。
        """
        done = [t for t in self.tasks.values()
                if t.status != "running" and t is not self._current]
        if len(done) <= self.MAX_HISTORY:
            return
        for old in sorted(done, key=lambda t: t.finished_at or 0)[:len(done) - self.MAX_HISTORY]:
            if old is self._current:
                continue
            self.tasks.pop(old.task_id, None)

    def get_task(self, task_id: str) -> SyncTask | None:
        return self.tasks.get(task_id)

    def last_finished(self) -> SyncTask | None:
        """最近一次已完成的任务（前端刷新后可以据此接着展示日志）。"""
        done = [t for t in self.tasks.values() if t.status != "running"]
        return max(done, key=lambda t: t.finished_at or 0) if done else None

    def _finish(self, task: SyncTask):
        task.status = "finished"
        task.finished_at = time.time()
        with self._lock:
            self._current = None
        # 钩子在锁外调用：避免回调里再访问同步器时发生死锁
        for fn in list(self._finish_hooks):
            try:
                fn(task)
            except Exception:  # noqa: BLE001  钩子异常不得影响任务收尾
                pass

    # ------------------------- 日志 / 降级 -------------------------
    @staticmethod
    def _logger(task: SyncTask, prefix: str = "   "):
        """带节流的日志回调：连续相似的进度行折叠为一行并计数。

        批量下载大 Mod 时 steamcmd 每秒输出大量进度行，全部入库会撑爆内存
        并拖慢前端轮询；折叠后只保留最新一行 + 重复次数。
        """
        state = {"key": None, "n": 0}

        def _cb(line: str):
            key = re.sub(r"\d+", "#", line)[:100]
            if state["key"] == key:
                state["n"] += 1
                # 就地更新上一行并换新 seq：客户端按 replace 原地刷新，计数不会滞后
                task.log(f"{prefix} {line}  ×{state['n'] + 1}", replace_last=True)
                return
            state["key"] = key
            state["n"] = 0
            task.log(f"{prefix} {line}")

        return _cb

    def _download_one_by_one(
        self, task: SyncTask, steam: SteamCMD, appid: str, group: list[dict],
        install_dir: Path, login_kw: dict, game_name: str,
        prefix: str = "   ", reason: str = "",
    ):
        """批量失败后的兜底：逐个下载，单个失败不影响其它 Mod。"""
        for t in group:
            itemid = t["itemid"]
            name = t["name"] or itemid
            label = f"[{game_name}] {name}"
            t0 = time.time()
            try:
                res = steam.download_item(
                    appid, itemid, str(install_dir),
                    log_callback=self._logger(task, prefix), **login_kw,
                )
                errs = res.get("errors") or []
            except SteamCMDError as e:
                task.log(f"✗ 失败: {label} -> {e}")
                task.results.append({
                    "appid": appid, "itemid": itemid, "name": name,
                    "ok": False, "reason": f"{reason}；单个重试: {e}" if reason else str(e),
                })
                continue
            if self.item_dir(appid, itemid).exists() and any(
                self.item_dir(appid, itemid).iterdir()
            ):
                task.log(f"✓ 完成: {label}（单个重试，耗时 {time.time() - t0:.0f}s）")
                task.results.append({
                    "appid": appid, "itemid": itemid, "name": name, "ok": True,
                })
            else:
                why = "; ".join(errs) or "未找到下载目录"
                task.log(f"✗ 失败: {label} -> {why}")
                task.results.append({
                    "appid": appid, "itemid": itemid, "name": name,
                    "ok": False, "reason": f"{reason}；{why}" if reason else why,
                })

    def _execute(self, task: SyncTask, targets: list[dict]):
        """任务线程入口。

        必须保证任何情况下都会走到 `_finish`：否则 `_current` 永远不释放，
        界面上的任务会一直显示「运行中」，日志跟随也会跟着卡死。
        """
        try:
            self._run(task, targets)
        except Exception as e:  # noqa: BLE001
            task.error = f"{type(e).__name__}: {e}"
            task.log(f"❌ 任务异常终止: {e}")
            self._finish(task)

    def _run(self, task: SyncTask, targets: list[dict]):
        settings = self.config.get_settings()
        # 注意：steamcmd 不走本项目代理配置（[proxy] 仅用于 Python 侧 Steam 网页访问）
        steam = SteamCMD(settings["steamcmd_path"])
        steam_acct = self.config.get_steam_account()
        use_account = steam_acct.get("enabled") and steam_acct.get("username")
        if use_account:
            task.log(f"使用 Steam 账号登录: {steam_acct['username']}")
        else:
            task.log("使用匿名登录（公开 Mod）")

        task.log(f"steamcmd 路径: {settings['steamcmd_path']}")
        if not steam.exists():
            task.log("❌ 未找到 steamcmd，请在「设置」中配置正确路径")
            self._finish(task)
            return

        if not targets:
            task.log("⚠️ 没有需要更新的 Mod")
            self._finish(task)
            return

        # 按游戏(appid)分组 -> 每组只登录一次、批量下载该组全部 Mod
        groups: dict[str, list[dict]] = {}
        for t in targets:
            groups.setdefault(str(t["appid"]), []).append(t)

        total = len(targets)
        task.log(
            f"开始更新，共 {total} 个 Mod，分 {len(groups)} 批"
            f"（每批一次登录、批量下载）"
        )
        done = 0
        login_kw = {
            "username": steam_acct.get("username") if use_account else None,
            "password": steam_acct.get("password") if use_account else None,
            "guard_code": steam_acct.get("guard_code") if use_account else None,
        }

        for appid, group in groups.items():
            install_dir = self.install_dir_for(appid)
            game_name = group[0].get("game_name") or appid
            # 分块：既享受「一次登录下载多个」，又避免单个大 Mod 卡死拖垮整批
            chunks = [group[i:i + self.BATCH_CHUNK]
                      for i in range(0, len(group), self.BATCH_CHUNK)]
            if len(chunks) > 1:
                task.log(
                    f"[{game_name}] 共 {len(group)} 个 Mod，分 {len(chunks)} 小批"
                    f"（每批最多 {self.BATCH_CHUNK} 个，失败自动降级逐个重试）"
                )
            for ci, chunk in enumerate(chunks, 1):
                itemids = [t["itemid"] for t in chunk]
                prefix = f"   [{ci}/{len(chunks)}]" if len(chunks) > 1 else "   "
                task.log(
                    f"▶ 批量下载 [{game_name}] 第 {ci}/{len(chunks)} 批，"
                    f"{len(itemids)} 个 Mod (appid={appid})，登录一次"
                )
                t0 = time.time()
                try:
                    res = steam.download_items(
                        appid, itemids, str(install_dir),
                        log_callback=self._logger(task, prefix), **login_kw,
                    )
                    batch_errors = res.get("errors", [])
                except SteamCMDError as e:
                    # 整批失败（多为登录/网络/超时）→ 降级为逐个下载，避免一损俱损
                    task.log(
                        f"⚠ 第 {ci} 批批量失败（{e}），"
                        f"耗时 {time.time() - t0:.0f}s，降级为逐个下载重试"
                    )
                    self._download_one_by_one(
                        task, steam, appid, chunk, install_dir, login_kw,
                        game_name, prefix, reason=str(e),
                    )
                    done += len(chunk)
                    task.log(f"进度: {done}/{total}")
                    continue
                task.log(f"第 {ci} 批结束，耗时 {time.time() - t0:.0f}s")

                # 批结束后按产物目录逐个判定成败
                for t in chunk:
                    itemid = t["itemid"]
                    name = t["name"] or itemid
                    label = f"[{game_name}] {name}"
                    item_path = self.item_dir(appid, itemid)
                    if item_path.exists() and any(item_path.iterdir()):
                        task.log(f"✓ 完成: {label}")
                        task.results.append({
                            "appid": appid, "itemid": itemid,
                            "name": name, "ok": True,
                        })
                    else:
                        # 优先归因到含该 itemid 的错误行，否则用整批聚合错误
                        hits = [e for e in batch_errors if itemid in e]
                        reason = "; ".join(hits or batch_errors) or "未找到下载目录"
                        task.log(f"✗ 失败: {label} -> {reason}")
                        task.results.append({
                            "appid": appid, "itemid": itemid,
                            "name": name, "ok": False, "reason": reason,
                        })
                    done += 1
                task.log(f"进度: {done}/{total}")

        task.log("更新结束")
        self._finish(task)

    # ------------------------- 清单 -------------------------
    def scan_manifest(self) -> list[dict]:
        """扫描本地已下载 Mod，生成清单。"""
        manifest: list[dict] = []
        games = {g["appid"]: g for g in self.config.get_games()}
        for appid, g in games.items():
            content_dir = self.content_dir_for(appid)
            if not content_dir.exists():
                continue
            for item_dir in sorted(content_dir.iterdir()):
                if not item_dir.is_dir():
                    continue
                itemid = item_dir.name
                files: list[dict] = []
                total_size = 0
                for f in item_dir.rglob("*"):
                    if f.is_file():
                        sz = f.stat().st_size
                        total_size += sz
                        files.append({
                            "name": str(f.relative_to(item_dir)).replace("\\", "/"),
                            "size": sz,
                        })
                mod_meta = next(
                    (m for m in g["mods"] if str(m["itemid"]) == itemid), {}
                )
                manifest.append({
                    "appid": appid,
                    "itemid": itemid,
                    "name": mod_meta.get("name", ""),
                    "file_count": len(files),
                    "total_size": total_size,
                    "files": files,
                })
        return manifest
