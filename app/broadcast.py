#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
局域网广播服务（复用 demo 思路）

周期性向局域网广播本服务器的 HTTP 端口，便于客户端自动发现。
"""

import json
import socket
import threading


class BroadcastService:
    """UDP 局域网广播。"""

    def __init__(self, http_port: int, broadcast_port: int, interval: int = 5):
        self.http_port = http_port
        self.broadcast_port = broadcast_port
        self.interval = interval
        self.running = False
        self.thread = None
        self.socket = None

    def start(self):
        self.running = True
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def stop(self):
        self.running = False
        if self.socket:
            try:
                self.socket.close()
            except Exception:  # noqa: BLE001
                pass

    def _loop(self):
        try:
            self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        except Exception as e:  # noqa: BLE001
            print(f"广播服务启动失败: {e}")
            return

        message = json.dumps({
            "type": "gamesync_server",
            "port": self.http_port,
        }).encode("utf-8")

        while self.running:
            try:
                self.socket.sendto(message, ("<broadcast>", self.broadcast_port))
            except Exception as e:  # noqa: BLE001
                print(f"广播发送失败: {e}")
            threading.Event().wait(self.interval)
