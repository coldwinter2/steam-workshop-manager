#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
局域网服务端自动发现（客户端）

监听服务端 UDP 广播（type=gamesync_server），返回其 HTTP 端口，
据此拼出 server_url。硬编码默认地址不可达时用它兜底。

与服务端 app/broadcast.py 的广播报文格式保持一致。
"""

import json
import socket
import threading
from typing import Callable, Optional


def parse_broadcast(data: bytes) -> Optional[int]:
    """解析一条广播报文，成功返回服务端 HTTP 端口，否则 None。"""
    try:
        msg = json.loads(data.decode("utf-8"))
    except Exception:  # noqa: BLE001
        return None
    if msg.get("type") == "gamesync_server" and msg.get("port"):
        try:
            return int(msg["port"])
        except (TypeError, ValueError):
            return None
    return None


def discover_server(
    broadcast_port: int,
    timeout: float = 15,
    on_found: Optional[Callable[[str, int], None]] = None,
) -> Optional[tuple[str, int]]:
    """阻塞监听广播直到超时；发现则返回 (host, http_port)。

    on_found(host, port) 为可选回调（发现即触发，可提前结束等待）。
    """
    result: dict = {"value": None}
    done = threading.Event()

    def _handle(host: str, port: int):
        result["value"] = (host, port)
        done.set()
        if on_found:
            on_found(host, port)

    def _listen():
        sock = None
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(("0.0.0.0", broadcast_port))
            sock.settimeout(1.0)
            start = __import__("time").time()
            while not done.is_set():
                if __import__("time").time() - start > timeout:
                    break
                try:
                    data, addr = sock.recvfrom(4096)
                except socket.timeout:
                    continue
                except Exception:  # noqa: BLE001
                    continue
                port = parse_broadcast(data)
                if port:
                    _handle(addr[0], port)
                    break
        finally:
            if sock:
                try:
                    sock.close()
                except Exception:  # noqa: BLE001
                    pass

    t = threading.Thread(target=_listen, daemon=True)
    t.start()
    done.wait(timeout)
    return result["value"]
