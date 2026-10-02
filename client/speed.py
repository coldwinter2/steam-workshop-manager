#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
下载速率计算与格式化（纯逻辑，不依赖 tkinter，便于单测）

组成：
  - fmt_rate()  统一单位换算：B/s → KB/s → MB/s → GB/s（1024 进制），
                并按数值量级自适应小数位，兼顾极低速与极高速，负值/NaN 归零。
  - SpeedMeter  速率计：由下载线程上报累计字节，UI 线程按**固定间隔**采样
                计算瞬时速率并做指数平滑；同时维护下载状态机，覆盖
                排队等待 / 暂停（无数据流）/ 失败重试 / 完成冻结 等边界情况。

两条速率（仅在压缩下载时有意义差异）：
  - effective（等效速率）：解压后的原始字节 / 秒 —— **主显示速度**
  - wire      （网络速率）：实际网络接收字节 / 秒 —— 压缩传输速率
  常规下载两者相等（未压缩，收到多少就是多少）。

线程约定：update() 由下载线程调用，sample() 由 UI 定时器调用；
只读写 int/float，CPython 下无需加锁。
"""

import time

# 采样与平滑参数
DEFAULT_INTERVAL = 0.5      # UI 采样间隔（秒）
DEFAULT_STALL_AFTER = 2.0   # 多久没有新数据判定为「暂停」
DEFAULT_SMOOTHING = 0.4     # EMA 平滑系数（越大越灵敏）

UNITS = ("B", "KB", "MB", "GB", "TB")


def _trim(s: str) -> str:
    """去掉尾随的 0 与小数点：4.10 -> 4.1、5.00 -> 5、50.0 -> 50。"""
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s or "0"


def fmt_rate(bps) -> str:
    """把字节/秒格式化为统一单位字符串（如 512 B/s、12.3 MB/s、1.25 GB/s）。

    精度自适应（并去掉无意义的尾随 0）：
      - <=0 / NaN / ±inf → "0 B/s"（速度为 0 等边界情况）
      - B 量级 <10       → 1 位小数，避免极低速被四舍五入成 0
      - 其余 <10         → 2 位小数（4.1 MB/s、1.25 GB/s）
      - <100             → 1 位小数（12.3 MB/s、45.6 KB/s）
      - >=100            → 取整（极高速时避免小数位抖动）
    """
    try:
        v = float(bps)
    except (TypeError, ValueError):
        v = 0.0
    if v != v or v in (float("inf"), float("-inf")):   # NaN / ±inf
        v = 0.0
    if v <= 0:
        return f"0 {UNITS[0]}/s"

    unit = 0
    while v >= 1024 and unit < len(UNITS) - 1:
        v /= 1024.0
        unit += 1

    if unit == 0:                       # B/s
        s = _trim(f"{v:.1f}") if v < 10 else f"{v:.0f}"
    elif v < 10:
        s = _trim(f"{v:.2f}")
    elif v < 100:
        s = _trim(f"{v:.1f}")
    else:
        s = f"{v:.0f}"
    return f"{s} {UNITS[unit]}/s"


def fmt_rate_pair(effective, wire) -> str:
    """两条速率的紧凑并列展示（列表中一行放不下完整文案时使用）。"""
    return f"{fmt_rate(effective)} / {fmt_rate(wire)}"


class RateSnapshot:
    """一次采样结果（浮点速率，单位统一为字节/秒）。"""

    __slots__ = ("effective", "wire", "state", "attempt", "elapsed")

    def __init__(self, effective=0.0, wire=0.0, state="idle",
                 attempt=1, elapsed=0.0):
        self.effective = effective
        self.wire = wire
        self.state = state
        self.attempt = attempt
        self.elapsed = elapsed

    def __repr__(self):  # pragma: no cover - 调试用
        return (f"RateSnapshot(effective={self.effective:.1f}, "
                f"wire={self.wire:.1f}, state={self.state}, "
                f"attempt={self.attempt})")


class SpeedMeter:
    """按固定间隔采样的速率计 + 下载状态机。

    状态：
      IDLE     未开始
      WAITING  排队等待：已开始但尚未收到任何数据
      ACTIVE   正在传输
      STALLED  暂停：超过 stall_after 没有新数据（网络中断/服务端无响应）
      RETRY    失败重试中（由 mark_retry 显式标记，直到新数据到达）
      DONE     已完成：速率冻结，不再跳动
    """

    IDLE = "idle"
    WAITING = "waiting"
    ACTIVE = "active"
    STALLED = "stalled"
    RETRY = "retry"
    DONE = "done"

    def __init__(self, interval: float = DEFAULT_INTERVAL,
                 stall_after: float = DEFAULT_STALL_AFTER,
                 smoothing: float = DEFAULT_SMOOTHING,
                 clock=time.monotonic):
        self.interval = interval
        self.stall_after = stall_after
        self.smoothing = smoothing
        self._clock = clock
        self.attempt = 1
        self.reset()

    # ---------------- 生命周期 ----------------
    def reset(self, now=None):
        """开始新任务（或新的一个 Mod）：计数与速率全部归零。"""
        now = self._clock() if now is None else now
        self.done = 0            # 累计：解压后原始字节
        self.wire = 0            # 累计：网络接收字节
        self.start = now
        self.last_sample = now
        self.last_progress = None   # 最后一次收到数据的时间
        self._s_done = 0
        self._s_wire = 0
        self.effective_rate = 0.0
        self.wire_rate = 0.0
        self.state = self.WAITING
        self.frozen = False
        self.attempt = 1

    def finish(self, now=None):
        """任务结束：冻结当前速率，后续 update/sample 不再改变它。"""
        if self.state != self.DONE:
            self.state = self.DONE
        self.frozen = True

    def mark_retry(self, attempt: int = 2, now=None):
        """失败重试：计数归零（不把失败前的字节算进速率），并标记重试态。"""
        self.reset(now=now)
        self.attempt = max(2, int(attempt))
        self.state = self.RETRY

    # ---------------- 数据上报（下载线程） ----------------
    def update(self, done, wire=None, now=None):
        """上报累计字节数。wire 省略时视为与 done 相同（常规下载）。"""
        if self.frozen:
            return
        now = self._clock() if now is None else now
        try:
            d = int(done)
        except (TypeError, ValueError):
            return
        w = d if wire is None else wire
        try:
            w = int(w)
        except (TypeError, ValueError):
            w = d
        if d > self.done:
            self.done = d
        if w > self.wire:
            self.wire = w
        if self.done > 0:
            self.last_progress = now
            if self.state in (self.IDLE, self.WAITING, self.STALLED, self.RETRY):
                self.state = self.ACTIVE

    # ---------------- 采样（UI 定时器） ----------------
    def sample(self, now=None) -> RateSnapshot:
        """按固定间隔调用，返回平滑后的速率与当前状态。"""
        now = self._clock() if now is None else now
        if self.frozen:
            return RateSnapshot(self.effective_rate, self.wire_rate,
                                self.DONE, self.attempt,
                                max(0.0, now - self.start))

        dt = now - self.last_sample
        if dt <= 0:
            # 同一时刻重复采样：沿用上次速率，避免除零造成的抖动
            return RateSnapshot(self.effective_rate, self.wire_rate,
                                self.state, self.attempt,
                                max(0.0, now - self.start))

        d_done = max(0, self.done - self._s_done)
        d_wire = max(0, self.wire - self._s_wire)
        inst_eff = d_done / dt
        inst_wire = d_wire / dt
        a = self.smoothing
        self.effective_rate = a * inst_eff + (1 - a) * self.effective_rate
        self.wire_rate = a * inst_wire + (1 - a) * self.wire_rate
        self._s_done = self.done
        self._s_wire = self.wire
        self.last_sample = now

        # 状态判定
        if d_done > 0:
            self.state = self.ACTIVE
        elif self.last_progress is None:
            # 还没有任何数据：重试期间保持 RETRY，否则为排队等待
            self.state = self.RETRY if self.attempt > 1 else self.WAITING
        elif now - self.last_progress >= self.stall_after:
            self.state = self.STALLED

        if self.state in (self.WAITING, self.STALLED):
            # 速度为 0（等待 / 暂停）不留残留速率，直接归零
            self.effective_rate = 0.0
            self.wire_rate = 0.0

        return RateSnapshot(self.effective_rate, self.wire_rate,
                            self.state, self.attempt,
                            max(0.0, now - self.start))

    # ---------------- 辅助 ----------------
    @property
    def average_rate(self) -> float:
        """整个任务的平均等效速率（字节/秒），仅作参考展示。"""
        elapsed = max(0.0, self._clock() - self.start)
        return (self.done / elapsed) if elapsed > 0 else 0.0
