#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
服务端地址管理（客户端）

地址优先级（高 → 低）：
  P0  本地保存的发现地址（持久化在 client_config.json 的 last_server_url）
      —— 只要本地存在保存的地址就优先使用它。
  P1  硬编码默认地址 config.DEFAULT_SERVER_URL
      —— 仅当本地“没有任何保存地址”时才使用（保存地址不存在，而非不可达）。
  P2  局域网广播发现（服务端 UDP 广播 type=gamesync_server）
      —— 当当前应使用的固定地址（有保存地址则用保存地址，否则用默认地址）
         不可达时“按需”临时启用：打开监听，直到发现可用服务端；一旦获取到
         可用地址即保存为 P0 并关闭监听（广播功能停用）。

行为：
  1. 启动后自动按优先级连接，无需点击「连接」。
  2. 广播监听具有“按需启用、用后即停”的特性：
       - 保存地址存在且可达    → 使用保存地址，不启用广播
       - 保存地址存在但不可达  → 临时启用广播发现（不回落默认地址）
       - 无保存地址、默认可达  → 使用默认地址，不启用广播
       - 无保存地址、默认不可达→ 临时启用广播发现
       发现可用地址后立即保存为 P0 并关闭监听。
  3. 连接断开后自动按同样优先级重连。
  4. 重连会跳过大文件下载/删除进行中的时刻，避免任务中途换服务端。
"""

import socket
import threading
import time
from typing import Callable, Optional, Tuple

from . import discovery


class EndpointManager:
    """按优先级选择并维护服务端地址。"""

    SOURCE_DEFAULT = "default"        # 硬编码默认地址（仅无保存地址时用）
    SOURCE_SAVED = "saved"            # 本地保存的发现地址（优先）
    SOURCE_DISCOVERED = "discovered"  # 本次广播发现地址

    def __init__(
        self,
        default_url: str,
        broadcast_port: int,
        probe: Callable[[str], bool],
        connect: Callable[[str, str], bool],
        on_lost: Optional[Callable[[], None]] = None,
        on_broadcast: Optional[Callable[[str, int], None]] = None,
        on_discovering: Optional[Callable[[], None]] = None,
        load_saved: Optional[Callable[[], str]] = None,
        save_discovered: Optional[Callable[[str], None]] = None,
        is_busy: Optional[Callable[[], bool]] = None,
        first_timeout: float = 15,
        recheck_interval: float = 10,
    ):
        self.default_url = (default_url or "").rstrip("/")
        self.broadcast_port = broadcast_port
        self.probe = probe                      # 轻量探活
        self.connect = connect                  # 真正建立连接（拉清单），返回是否成功
        self.on_lost = on_lost
        self.on_broadcast = on_broadcast
        self.on_discovering = on_discovering
        self.load_saved = load_saved
        self.save_discovered = save_discovered
        self.is_busy = is_busy or (lambda: False)
        self.first_timeout = first_timeout
        self.recheck_interval = recheck_interval

        self.url = ""                # 当前生效地址
        self.source = ""             # 当前地址来源
        self.connected = False
        self.last_broadcast: Optional[Tuple[str, int]] = None
        # P0：启动时从持久化配置载入本地保存的发现地址（优先使用）
        self.saved_url = ((load_saved() if load_saved else "") or "").rstrip("/")

        self._sock = None
        self._thread = None
        self._stop = threading.Event()
        self._wake = threading.Event()

    # ---------------- 对外 ----------------
    def start(self):
        """启动后台线程：立即按优先级连接，后续周期性复核 / 自动重连。"""
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._wake.set()

    def wake(self):
        """请求立即复核一次（如手工点「重连」）。"""
        self._wake.set()

    def note_connected(self, url: str, source: str):
        self.url, self.source, self.connected = url, source, True

    def label(self) -> str:
        if self.source == self.SOURCE_DEFAULT:
            return "默认地址"
        if self.source == self.SOURCE_SAVED:
            return "已保存地址"
        return "广播发现"

    # ---------------- 内部 ----------------
    def _run(self):
        # 首轮：按优先级连接（保存地址 → 默认地址 → 广播发现）
        self._connect_chain()
        # 后续：周期性复核 / 等待手工唤醒
        while not self._stop.is_set():
            waited = 0.0
            while (not self._stop.is_set() and not self._wake.is_set()
                   and waited < self.recheck_interval):
                time.sleep(0.2)
                waited += 0.2
            self._wake.clear()
            if self._stop.is_set():
                break
            self._evaluate()
        self._close_socket()

    def _open_socket(self):
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("0.0.0.0", self.broadcast_port))
            sock.settimeout(0.5)
            self._sock = sock
        except Exception as e:  # noqa: BLE001
            print(f"[警告] 广播监听端口 {self.broadcast_port} 打开失败，"
                  f"将仅使用默认/已保存地址: {e}")
            self._sock = None

    def _close_socket(self):
        if self._sock:
            try:
                self._sock.close()
            except Exception:  # noqa: BLE001
                pass
            self._sock = None

    def _drain(self, timeout: float = 0.5):
        """收一包广播并记录最新地址（非阻塞，超时即返回）。"""
        if self._sock is None:
            return
        try:
            self._sock.settimeout(timeout)
            data, addr = self._sock.recvfrom(4096)
        except (socket.timeout, OSError):
            return
        except Exception:  # noqa: BLE001
            return
        port = discovery.parse_broadcast(data)
        if not port:
            return
        host = addr[0]
        if (host, port) != self.last_broadcast:
            self.last_broadcast = (host, port)
            if self.on_broadcast:
                self.on_broadcast(host, port)

    def _broadcast_url(self) -> str:
        if not self.last_broadcast:
            return ""
        host, port = self.last_broadcast
        return f"http://{host}:{port}"

    def _try(self, url: str, source: str) -> bool:
        if not url or not self.probe(url):
            return False
        if not self.connect(url, source):
            return False
        self.note_connected(url, source)
        return True

    def _try_default(self) -> bool:
        return self._try(self.default_url, self.SOURCE_DEFAULT)

    def _try_saved(self) -> bool:
        return self._try(self.saved_url, self.SOURCE_SAVED)

    def _save_discovered(self, url: str):
        """把本次广播发现的可用地址记为本地保存地址（优先使用）并持久化。"""
        self.saved_url = (url or "").rstrip("/")
        if self.save_discovered:
            try:
                self.save_discovered(self.saved_url)
            except Exception:  # noqa: BLE001
                pass

    def _connect_chain(self):
        """按优先级尝试：保存地址(P0) → 默认地址(P1,仅无保存时) → 广播发现(P2)。"""
        if self.is_busy():
            return
        if self.saved_url:
            # P0：本地存在保存地址 → 优先使用；不可达则走广播发现（不回落默认）
            if self._try_saved():
                return
        elif self._try_default():
            # P1：本地没有任何保存地址 → 使用硬编码默认地址
            return
        # P2：当前固定地址缺失/不可达 → 临时启用广播发现
        self._discover_via_broadcast()

    def _discover_via_broadcast(self):
        """临时启用广播监听，直到发现可用服务端或收到停止/唤醒信号。

        发现可用地址后立即保存为本地保存地址并关闭监听（“用后即停”）。
        若保存/默认地址在监听期间恢复，则通过唤醒信号提前结束监听、
        交由上层 _evaluate 重新走优先级链。
        """
        if self.on_discovering:
            self.on_discovering()
        self._open_socket()
        if self._sock is None:
            return
        try:
            while (not self._stop.is_set()
                   and not self._wake.is_set()
                   and not self.connected):
                self._drain(0.5)
                url = self._broadcast_url()
                if url and self._try(url, self.SOURCE_DISCOVERED):
                    self._save_discovered(url)
                    break
        finally:
            self._close_socket()
            self._wake.clear()

    def _evaluate(self):
        """周期复核：保持当前连接；断开则按优先级重连。"""
        if self.is_busy():
            return
        if self.connected:
            if self.probe(self.url):
                return  # 当前地址仍可达，保持现状
            self._mark_lost()
        # 未连接（或刚丢失）：按优先级连接
        self._connect_chain()

    def _mark_lost(self):
        if not self.connected:
            return
        self.connected = False
        if self.on_lost:
            self.on_lost()
