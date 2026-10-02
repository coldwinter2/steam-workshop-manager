#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
自动更新调度器

两种触发条件，均可独立开关（总开关关闭时全部不触发）：

1. **延时触发**：新增 Mod 后延迟 N 分钟（默认 5）触发一次更新；
   倒计时期间再次新增 Mod -> 合并为同一次触发并**重新计时**。
2. **定时扫描**：每间隔 M 小时（默认 1）扫描一次，发现「未下载」的 Mod 时才触发。
3. **手动触发**：`trigger_now()` 可由界面按钮调用，复用同一条触发路径。

共同约束：
  - **只处理尚未下载的 Mod**（已下载一律跳过，不覆盖、不强制更新、不重复下载）。
  - **同一时间只允许一个更新任务**；任务执行期间的触发请求直接跳过，不排队堆积。
  - 下载失败只记录，不影响下一次触发；失败明细保留在状态里供界面查看。
  - **任何更新任务完成后（自动 / 手动）都会重置计时**（清空延时倒计时、
    从完成时刻重排定时扫描），界面展示的「下次自动更新」随之刷新。
"""

import threading
import time

# 循环检查间隔（秒）：配置变更 / 新增 Mod 会唤醒线程，无需靠轮询
TICK_INTERVAL = 5.0


class AutoUpdater:
    """后台调度：按配置触发「仅未下载」的自动更新。"""

    def __init__(self, config, sync_manager):
        self.config = config
        self.sync = sync_manager
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None

        # 运行时状态
        self._pending_at: float | None = None      # 延时触发的目标时刻
        self._next_scan_at: float | None = None    # 下次定时扫描时刻
        self._last_scan_at: float | None = None
        self._last_scan_pending = 0
        self._last_run_at: float | None = None
        self._last_trigger: str | None = None      # 触发来源（延时触发 / 定时扫描）
        self._last_task_id: str | None = None
        self._last_ok = 0
        self._last_fail = 0
        self._last_failed_mods: list[dict] = []
        self._last_error: str | None = None
        self._skipped = 0                          # 因任务运行中而跳过的次数
        self._last_skip_at: float | None = None
        self._last_reset_at: float | None = None   # 最近一次「计时重置」时刻
        self._last_reset_reason: str | None = None
        self._notes: list[str] = []                # 简要运行记录（最多保留 20 条）

    # ------------------------- 配置 -------------------------
    def cfg(self) -> dict:
        return self.config.get_auto_update()

    # ------------------------- 生命周期 -------------------------
    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self.apply_config()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        # 启动后先扫描一次（只登记未下载数量并排期，**不在启动时下载**），
        # 真正的下载交给下一个到点的扫描周期；界面因此能立刻看到待下载数
        self._scan_once(trigger=False)

    def stop(self):
        self._stop.set()
        self._wake.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)

    def is_running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def apply_config(self):
        """配置变更后立即应用：重算扫描时刻、按需清理延时倒计时。"""
        c = self.cfg()
        with self._lock:
            if c["enabled"] and c["scan_enabled"]:
                self._next_scan_at = time.time() + c["scan_interval"] * 60
            else:
                self._next_scan_at = None
            if not (c["enabled"] and c["delay_enabled"]):
                self._pending_at = None
        self._wake.set()

    def notify_mods_added(self, appid: str = None, count: int = 0):
        """新增 Mod 后调用：合并为同一次触发并重新计时。"""
        c = self.cfg()
        if not (c["enabled"] and c["delay_enabled"]):
            return
        with self._lock:
            self._pending_at = time.time() + c["delay_minutes"] * 60
            fire = self._pending_at
        self._note(
            f"检测到新增 Mod{'（AppID ' + str(appid) + '）' if appid else ''}"
            f"，将在 {c['delay_minutes']:g} 分钟后触发"
            f"（{time.strftime('%H:%M:%S', time.localtime(fire))}）"
        )
        self._wake.set()

    # ------------------------- 主循环 -------------------------
    def _loop(self):
        while not self._stop.is_set():
            now = time.time()
            with self._lock:
                pending_at = self._pending_at
                scan_at = self._next_scan_at
            if pending_at is not None and now >= pending_at:
                with self._lock:
                    self._pending_at = None
                self._fire("延时触发（新增 Mod）")
            if scan_at is not None and now >= scan_at:
                with self._lock:
                    self._next_scan_at = None
                self._scan_once()
            self._wake.wait(TICK_INTERVAL)
            self._wake.clear()

    def _scan_once(self, trigger: bool = True):
        """扫描一次：仅在存在未下载 Mod 时才可能触发更新。

        trigger=True（默认）：定时扫描到点时调用，发现未下载即 `_fire()` 下载。
        trigger=False：服务启动时调用，**只登记未下载数量 + 排期（不在启动时
        触发）**，避免每次重启都立刻开跑下载；任务由下一个到点的扫描周期执行。
        """
        c = self.cfg()
        if not (c["enabled"] and c["scan_enabled"]):
            # 总开关或扫描开关关闭：不做扫描，也不排下次
            with self._lock:
                self._next_scan_at = None
            return
        with self._lock:
            self._last_scan_at = time.time()
        try:
            count = len(self.sync.pending_targets())
        except Exception as e:  # noqa: BLE001
            with self._lock:
                self._last_scan_pending = 0
                self._last_error = f"扫描未下载 Mod 失败: {e}"
            self._note(f"扫描失败: {e}")
            self._reschedule_scan()
            return
        with self._lock:
            self._last_scan_pending = count
        if count == 0:
            self._note("定时扫描：无未下载 Mod，跳过")
        elif trigger:
            self._note(f"定时扫描：发现 {count} 个未下载 Mod")
            self._fire("定时扫描")
        self._reschedule_scan()
        if count and not trigger:
            at = self._next_scan_at
            when = time.strftime("%H:%M:%S", time.localtime(at)) if at else "稍后"
            self._note(
                f"启动扫描：发现 {count} 个未下载 Mod，已排期至 {when} 触发"
                f"（启动时不在第一时间下载）"
            )

    def _reschedule_scan(self):
        c = self.cfg()
        with self._lock:
            self._next_scan_at = (
                time.time() + c["scan_interval"] * 60
                if c["enabled"] and c["scan_enabled"] else None
            )

    # ------------------------- 触发 -------------------------
    def trigger_now(self, reason: str = "手动触发（界面按钮）") -> dict:
        """界面按钮：立即执行一次「仅未下载」的更新流程。

        与 `_fire` 走同一条路径（同样调用 `sync_manager.start_pending()`），
        不新增任何重复的同步实现；区别仅在于**用户显式点击时不检查总开关**，
        但仍受「同一时间只允许一个更新任务」约束。

        返回结构::

            {"ok": bool, "task_id": str|None, "pending_count": int,
             "skipped": bool, "message": str}
        """
        return self._fire(reason, require_enabled=False)

    def _fire(self, reason: str, require_enabled: bool = True) -> dict:
        """触发一次「仅未下载」的更新。

        任务执行中 -> 直接跳过（不排队堆积），仅记录跳过次数。
        """
        c = self.cfg()
        if require_enabled and not c["enabled"]:
            return {"ok": False, "task_id": None, "pending_count": 0,
                    "skipped": False, "message": "自动更新未启用"}
        if self.sync.current_task_id() is not None:
            with self._lock:
                self._skipped += 1
                self._last_skip_at = time.time()
            self._note(f"跳过触发（{reason}）：已有更新任务在运行")
            return {"ok": False, "task_id": None, "pending_count": 0,
                    "skipped": True, "message": "已有更新任务正在运行，已跳过本次触发"}
        try:
            task_id, count = self.sync.start_pending(label=f"自动更新（{reason}）")
        except Exception as e:  # noqa: BLE001  配置缺失等
            with self._lock:
                self._last_error = str(e)
            self._note(f"触发失败（{reason}）: {e}")
            return {"ok": False, "task_id": None, "pending_count": 0,
                    "skipped": False, "message": f"启动失败: {e}"}
        if task_id is None:
            if count == 0:
                self._note(f"{reason}：没有未下载的 Mod，未启动任务")
                return {"ok": True, "task_id": None, "pending_count": 0,
                        "skipped": False, "message": "没有未下载的 Mod，无需更新"}
            with self._lock:
                self._skipped += 1
                self._last_skip_at = time.time()
            self._note(f"跳过触发（{reason}）：已有更新任务在运行")
            return {"ok": False, "task_id": None, "pending_count": count,
                    "skipped": True, "message": "已有更新任务正在运行，已跳过本次触发"}
        with self._lock:
            self._last_run_at = time.time()
            self._last_trigger = reason
            self._last_task_id = task_id
            self._last_ok = 0
            self._last_fail = 0
            self._last_failed_mods = []
            self._last_error = None
        self._note(f"已启动自动更新任务 {task_id}（{reason}，{count} 个未下载 Mod）")
        threading.Thread(target=self._watch, args=(task_id,), daemon=True).start()
        return {"ok": True, "task_id": task_id, "pending_count": count,
                "skipped": False, "message": f"已启动更新（{count} 个未下载 Mod）"}

    # ------------------------- 计时重置 -------------------------
    def reset_timers(self, reason: str = ""):
        """重置自动更新计时：清空延时倒计时 + 从当前时刻重排定时扫描。

        任何更新任务完成都会调用（见 `on_sync_finished`），使界面上的
        「下次自动更新」时间按最近一次更新结束时刻重新计算。
        配置的时间间隔本身不变，只是重新起算。
        """
        c = self.cfg()
        now = time.time()
        interval = c["scan_interval"] * 60
        with self._lock:
            had_delay = self._pending_at is not None
            self._pending_at = None
            self._next_scan_at = (
                now + interval if c["enabled"] and c["scan_enabled"] else None
            )
            self._last_reset_at = now
            self._last_reset_reason = reason or "更新任务完成"
        parts = [self._last_reset_reason]
        if had_delay:
            parts.append("清空延时倒计时")
        if self._next_scan_at:
            parts.append(
                "下次定时扫描 "
                + time.strftime("%H:%M:%S", time.localtime(self._next_scan_at))
            )
        else:
            parts.append("暂无定时扫描计划")
        self._note("已重置自动更新计时：" + "，".join(parts))
        self._wake.set()

    def on_sync_finished(self, task):
        """任务完成钩子：由 sync_manager 在任何同步任务结束时回调。

        无论来源是自动触发还是手动触发（界面按钮 / 更新全部），
        只要更新执行完成就重置自动更新计时。
        """
        try:
            results = list(getattr(task, "results", []) or [])
            ok = sum(1 for r in results if r.get("ok"))
            fail = len(results) - ok
            self.reset_timers(
                f"更新任务 {getattr(task, 'task_id', '')} 完成"
                f"（成功 {ok} / 失败 {fail}）"
            )
        except Exception as e:  # noqa: BLE001  钩子异常不得影响任务收尾
            self._note(f"计时重置失败: {e}")

    def _watch(self, task_id: str):
        """等待任务结束并记录结果（失败明细供界面查看）。"""
        task = self.sync.get_task(task_id)
        if task is None:
            return
        while task.status == "running":
            if self._stop.is_set():
                return
            time.sleep(2)
        ok = [r for r in task.results if r.get("ok")]
        fail = [r for r in task.results if not r.get("ok")]
        with self._lock:
            self._last_ok = len(ok)
            self._last_fail = len(fail)
            self._last_failed_mods = [
                {"appid": r.get("appid", ""), "itemid": r.get("itemid", ""),
                 "name": r.get("name") or r.get("itemid", ""),
                 "reason": r.get("reason", "")}
                for r in fail
            ]
        if fail:
            self._note(
                f"自动更新任务 {task_id} 结束：成功 {len(ok)}，失败 {len(fail)}"
                f"（{self._last_failed_mods[0]['name']} 等）"
            )
        else:
            self._note(f"自动更新任务 {task_id} 结束：成功 {len(ok)}")

    # ------------------------- 状态 -------------------------
    def _note(self, msg: str):
        ts = time.strftime("%H:%M:%S")
        with self._lock:
            self._notes.append(f"[{ts}] {msg}")
            if len(self._notes) > 20:
                del self._notes[0]

    def status(self) -> dict:
        """供界面展示的完整状态（含下次触发时间与剩余倒计时）。"""
        c = self.cfg()
        now = time.time()
        with self._lock:
            pending_at = self._pending_at
            scan_at = self._next_scan_at
            active = c["enabled"] and (
                (c["delay_enabled"] and pending_at is not None)
                or (c["scan_enabled"] and scan_at is not None)
            )
            next_at = None
            next_kind = None
            cands = []
            if c["enabled"] and c["delay_enabled"] and pending_at:
                cands.append((pending_at, "延时触发"))
            if c["enabled"] and c["scan_enabled"] and scan_at:
                cands.append((scan_at, "定时扫描"))
            if cands:
                next_at, next_kind = min(cands, key=lambda x: x[0])
        try:
            pending_count = len(self.sync.pending_targets())
        except Exception:  # noqa: BLE001
            pending_count = 0
        with self._lock:
            return {
                "config": c,
                "scheduler_running": self.is_running(),
                "enabled": c["enabled"],
                "active": active,
                "sync_running": self.sync.current_task_id() is not None,
                "pending_count": pending_count,
                "delay_fire_at": pending_at,
                "delay_remaining": max(0.0, pending_at - now) if pending_at else None,
                "next_scan_at": scan_at,
                "next_scan_remaining": max(0.0, scan_at - now) if scan_at else None,
                "next_fire_at": next_at,
                "next_fire_in": max(0.0, next_at - now) if next_at else None,
                "next_fire_kind": next_kind,
                "last_scan_at": self._last_scan_at,
                "last_scan_pending": self._last_scan_pending,
                "last_run_at": self._last_run_at,
                "last_trigger": self._last_trigger,
                "last_task_id": self._last_task_id,
                "last_ok": self._last_ok,
                "last_fail": self._last_fail,
                "last_failed_mods": list(self._last_failed_mods),
                "last_error": self._last_error,
                "skipped": self._skipped,
                "last_skip_at": self._last_skip_at,
                "last_reset_at": self._last_reset_at,
                "last_reset_reason": self._last_reset_reason,
                "notes": list(self._notes),
            }
