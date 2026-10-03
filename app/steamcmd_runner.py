#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
steamcmd 封装

- 检测 steamcmd 是否存在
- 支持匿名或账号登录（含 Steam Guard 设备授权码 +set_steam_guard_code）
- 下载指定创意工坊 Mod（workshop_download_item）
- login_test：尝试登录并判断是否需要设备授权（首次登录常需信任设备）
- run_script：以 +runscript 方式执行一段脚本文本（先落盘为临时脚本文件再执行）
"""

import os
import queue
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path

from . import ansi as _ansi

# ---------------- 超时策略 ----------------
# 说明：旧实现的 timeout 只作用于 proc.wait()，而真正阻塞的是「读 stdout 直到
# EOF」，因此批量下载慢/卡死时 timeout 形同虚设（既不超时也不结束）。
# 现改为双超时：
#   DEFAULT_TIMEOUT      —— 总墙钟上限（从进程启动开始计时）
#   DEFAULT_IDLE_TIMEOUT —— 空闲上限：超过这么久没有任何新输出即判定卡死
DEFAULT_TIMEOUT = 1800        # 单批总墙钟上限（秒）
DEFAULT_IDLE_TIMEOUT = 600    # 无新输出判死（秒；留足余量避免误杀大文件静默下载）
PER_ITEM_TIMEOUT = 600        # 批量时每个 item 追加的墙钟时间（秒）

# 疑似需要 Steam Guard / 设备授权的关键字
_GUARD_KEYWORDS = (
    "steam guard", "guard code", "enter the current code",
    "two-factor", "two factor", "please enter", "authenticator",
)
# 明确失败关键字
_FAIL_KEYWORDS = ("login failure", "account login denied", "invalid password", "no match")


class SteamCMDError(Exception):
    """steamcmd 调用相关错误。"""


def _login_args(username: str = None, password: str = None, guard_code: str = None):
    if username:
        args = ["+login", username, password]
        if guard_code:
            args += ["+set_steam_guard_code", guard_code]
        return args
    return ["+login", "anonymous"]


def _script_quote(value) -> str:
    """脚本参数加引号：仅含空格/制表符时加，并把反斜杠统一为正斜杠。

    steamcmd 脚本按空格切分参数，路径含空格必须用引号包裹；引号内的
    反斜杠有被当作转义符的风险，故统一成正斜杠（steamcmd 接受 `/` 路径）。
    """
    v = "" if value is None else str(value)
    if v and not any(c in v for c in (" ", "\t")):
        return v
    return '"' + v.replace("\\", "/").replace('"', "") + '"'


def _login_script_lines(username: str = None, password: str = None,
                        guard_code: str = None) -> list:
    """与 _login_args 等价的**脚本行**形式（不带 `+` 前缀）。"""
    if username:
        lines = [f"login {_script_quote(username)} {_script_quote(password)}"]
        if guard_code:
            lines.append(f"set_steam_guard_code {_script_quote(guard_code)}")
        return lines
    return ["login anonymous"]


class SteamCMD:
    """对 steamcmd 可执行文件的薄封装。"""

    def __init__(self, exe_path: str):
        # 注意：steamcmd 子进程**不使用**本项目的代理配置（[proxy] 仅作用于
        # Python 侧的 Steam 网页/API 访问）。这里不注入任何代理环境变量，
        # 保持 steamcmd 自身的网络行为（继承系统环境）。
        self.exe_path = Path(exe_path)

    # ------------------------- 检测 -------------------------
    def exists(self) -> bool:
        return self._resolve().exists()

    def _resolve(self) -> Path:
        p = self.exe_path
        if p.is_dir():
            for cand in ("steamcmd.exe", "steamcmd.sh"):
                c = p / cand
                if c.exists():
                    return c
        return p

    def resolve(self) -> Path:
        """公开版路径解析：供外部模块定位 steamcmd 根目录（如查找 ACF）。"""
        return self._resolve()

    def version(self) -> str:
        exe = self._resolve()
        if not exe.exists():
            raise SteamCMDError(f"steamcmd 不存在: {exe}")
        try:
            out = subprocess.run(
                [str(exe), "+version", "+quit"],
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=60,
            )
            return (out.stdout or out.stderr or "").strip()
        except subprocess.TimeoutExpired:
            return "(获取版本超时)"
        except Exception as e:  # noqa: BLE001
            return f"(获取版本失败: {e})"

    # ------------------------- 内部执行 -------------------------
    @staticmethod
    def _kill(proc):
        """终止进程及其子进程树（steamcmd 会拉起子进程，只 kill 主进程不够）。"""
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass
        if os.name == "nt":
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                    capture_output=True, timeout=10,
                )
            except Exception:  # noqa: BLE001
                pass

    @staticmethod
    def _tail(logs: list, n: int = 5) -> str:
        return " | ".join(x.strip() for x in logs[-n:]) or "(无输出)"

    def _run(
        self, cmd: list, log_callback=None,
        timeout: int = DEFAULT_TIMEOUT, idle_timeout: int = DEFAULT_IDLE_TIMEOUT,
    ) -> dict:
        """执行命令并流式收集日志。

        超时语义（关键修复）：
          - 旧实现 `for line in proc.stdout` 会一直阻塞到 EOF，timeout 只作用于
            之后的 `proc.wait()`，因此**下载慢或卡死时永远不会超时**。
          - 现在：读线程把行放进队列，主线程按「总墙钟 timeout」与
            「空闲 idle_timeout（无新输出）」双重判定，超时即杀进程树并报错。
        """
        exe = self._resolve()
        if not exe.exists():
            raise SteamCMDError(f"steamcmd 不存在: {exe}")
        logs: list[str] = []
        errors: list[str] = []
        try:
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1, encoding="utf-8", errors="replace",
            )
        except Exception as e:  # noqa: BLE001
            raise SteamCMDError(f"启动 steamcmd 失败: {e}") from e

        assert proc.stdout is not None
        q: "queue.Queue[str | None]" = queue.Queue()

        def _reader():
            try:
                for line in proc.stdout:
                    q.put(line.rstrip("\r\n"))
            except Exception:  # noqa: BLE001
                pass
            finally:
                try:
                    proc.stdout.close()
                except Exception:  # noqa: BLE001
                    pass
                q.put(None)  # EOF 哨兵

        threading.Thread(target=_reader, daemon=True).start()

        deadline = time.time() + max(1, int(timeout))
        idle = max(1, int(idle_timeout))
        last_out = time.time()
        eof = False
        while not eof:
            # 每轮都校验总墙钟（不能只在队列为空时校验：持续输出的下载
            # 场景永远不会空闲，否则总超时会被「绕过」）
            if time.time() > deadline:
                self._kill(proc)
                raise SteamCMDError(
                    f"执行超时（>{timeout}s）｜最近日志: {self._tail(logs)}"
                )
            try:
                item = q.get(timeout=0.5)
            except queue.Empty:
                now = time.time()
                if now - last_out > idle:
                    self._kill(proc)
                    raise SteamCMDError(
                        f"执行无响应：{idle}s 内无任何输出（判定卡死）"
                        f"｜最近日志: {self._tail(logs)}"
                    )
                continue
            if item is None:
                eof = True
                break
            last_out = time.time()
            # 关键：steamcmd 在部分平台（如 Ubuntu）会向 stdout 写入 ANSI 颜色/
            # 样式序列（ESC[0m / ESC[1m 等）。这些序列进入日志后在终端里才表现为
            # 颜色，一旦被管道捕获、写入日志文件或 systemd journal 就会变成可见的
            # [0m [1m 乱码。统一在此处剔除，保证最终落库的日志是纯文本。
            line = _ansi.strip_ansi(item)
            if not line:
                # 原行全是转义序列、去净后为空 -> 跳过（无实际内容）
                continue
            logs.append(line)
            low = line.lower()
            if any(k in low for k in _FAIL_KEYWORDS) or "error" in low:
                errors.append(line)
            if log_callback:
                log_callback(line)

        remain = max(1.0, min(10.0, deadline - time.time()))
        try:
            proc.wait(timeout=remain)
        except subprocess.TimeoutExpired:
            self._kill(proc)
            raise SteamCMDError(
                f"输出已结束但进程未退出（>{timeout}s）｜最近日志: {self._tail(logs)}"
            )
        return {"returncode": proc.returncode, "errors": errors, "logs": logs}

    # ------------------------- 登录测试 -------------------------
    def login_test(
        self, username: str = None, password: str = None,
        guard_code: str = None, log_callback=None, timeout: int = 180,
    ) -> dict:
        """尝试登录，判断是否需要进行设备授权（Steam Guard）。"""
        cmd = [str(self._resolve())] + _login_args(username, password, guard_code) + ["+quit"]
        res = self._run(cmd, log_callback, timeout)
        joined = " ".join(res["logs"]).lower()
        needs_guard = any(k in joined for k in _GUARD_KEYWORDS)
        hard_fail = any(k in joined for k in _FAIL_KEYWORDS)
        ok = (not needs_guard) and (not hard_fail) and res["returncode"] == 0
        return {
            "ok": ok,
            "needs_guard_code": needs_guard,
            "logs": res["logs"],
            "errors": res["errors"],
        }

    # ------------------------- 下载 -------------------------
    def download_item(
        self, appid: str, itemid: str, install_dir: str,
        username: str = None, password: str = None, guard_code: str = None,
        log_callback=None, timeout: int = DEFAULT_TIMEOUT,
        idle_timeout: int = DEFAULT_IDLE_TIMEOUT,
    ) -> dict:
        exe = self._resolve()
        if not exe.exists():
            raise SteamCMDError(f"steamcmd 不存在: {exe}")

        install_path = Path(install_dir)
        install_path.mkdir(parents=True, exist_ok=True)

        # force_install_dir 必须排在 login 之前（与 download_items 的脚本顺序一致）：
        # 否则 steamcmd 会报 "Please use force_install_dir before logon!" 并可能忽略该设置
        cmd = (
            [str(exe)]
            + ["+force_install_dir", str(install_path)]
            + _login_args(username, password, guard_code)
            + ["+workshop_download_item", str(appid), str(itemid), "+quit"]
        )
        return self._run(cmd, log_callback, timeout, idle_timeout)

    def download_items(
        self,
        appid: str,
        itemids: list,
        install_dir: str,
        username: str = None, password: str = None, guard_code: str = None,
        log_callback=None, timeout: int = None,
        idle_timeout: int = DEFAULT_IDLE_TIMEOUT,
    ) -> dict:
        """**批量下载**：一次登录，同一会话内顺序执行多个 workshop_download_item。

        避免每个 Mod 单独起一次 steamcmd（每次都重新登录，慢且易触发限流）。

        执行方式：**以 `+runscript` 脚本文件方式执行**（原先是拼接命令行参数）。
        好处是不受命令行长度限制、无需担心参数转义，脚本内容即完整可复查的
        执行计划。生成的脚本形如::

            force_install_dir D:/mods
            login anonymous
            workshop_download_item 550 2807672660
            workshop_download_item 550 2807678136
            quit

        注意顺序：**`force_install_dir` 必须在 `login` 之前**——否则 steamcmd
        会打印 `Please use force_install_dir before logon!` 并可能忽略该设置
        （旧实现把 login 拼在前面，存在此隐患）。

        登录行由 `_login_script_lines()` 生成（匿名或账号 + 可选 Guard 码），
        `force_install_dir` 的路径含空格时自动加引号（见 `_script_quote`）。

        返回结构同 _run（额外含 `script_path`）；各 item 成败由调用方按产物目录
        逐个判定（批量日志的 errors 为整体聚合，无法精确归因到单个 item）。

        超时：未指定时 = DEFAULT_TIMEOUT + PER_ITEM_TIMEOUT × item 数
        （旧实现是 max(1800, 300×n)，对 300MB 级别的大 Mod 明显不够）。
        另有 idle_timeout：长时间无输出即判定卡死，避免整批永久挂起。
        """
        exe = self._resolve()
        if not exe.exists():
            raise SteamCMDError(f"steamcmd 不存在: {exe}")

        itemids = [str(i) for i in itemids if str(i)]
        if not itemids:
            return {"returncode": 0, "errors": [], "logs": [], "script_path": ""}

        install_path = Path(install_dir)
        install_path.mkdir(parents=True, exist_ok=True)

        # 路径统一用正斜杠：steamcmd 在 Windows 上同样接受 `/`，且可避免脚本里
        # 反斜杠被当成转义符（含空格时再由 _script_quote 加引号）
        lines = [
            f"force_install_dir {_script_quote(str(install_path).replace(chr(92), '/'))}"
        ]
        # force_install_dir 必须排在 login 之前（否则 steamcmd 报
        # "Please use force_install_dir before logon!" 并可能忽略该设置）
        lines += _login_script_lines(username, password, guard_code)
        for iid in itemids:
            lines.append(f"workshop_download_item {appid} {iid} validate")
        script = "\n".join(lines)

        if timeout is None:
            timeout = DEFAULT_TIMEOUT + PER_ITEM_TIMEOUT * len(itemids)
        return self.run_script(script, log_callback, timeout, idle_timeout)

    # ------------------------- 脚本执行（+runscript） -------------------------
    @staticmethod
    def _normalize_script(script_content: str) -> str:
        """规范化脚本文本，使其符合 steamcmd 脚本文件格式。

        脚本文件里每行写一条命令且**不带 `+` 前缀**（与命令行参数写法不同），
        这里做三件事：统一换行符、去掉行首多余的 `+`、丢弃空行。
        """
        text = (script_content or "").replace("\r\n", "\n").replace("\r", "\n")
        lines = []
        for raw in text.split("\n"):
            line = raw.strip()
            if not line:
                continue
            if line.startswith("+"):
                line = line[1:].strip()
            if line:
                lines.append(line)
        return "\n".join(lines)

    @staticmethod
    def _has_quit(script: str) -> bool:
        """脚本末尾是否已有 quit（避免重复追加导致 steamcmd 报未知命令）。"""
        return any(line.strip().lower() == "quit" for line in script.split("\n"))

    def _write_script(self, script: str, script_dir: str = None) -> Path:
        """把脚本文本写入临时脚本文件，返回其路径。"""
        directory = Path(script_dir) if script_dir else Path(tempfile.gettempdir())
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"gamesync_script_{uuid.uuid4().hex[:8]}.txt"
        # steamcmd 读取脚本使用系统本地换行；UTF-8 不带 BOM（带 BOM 会污染首行命令）
        newline = "\r\n" if os.name == "nt" else "\n"
        with open(path, "w", encoding="utf-8", newline=newline) as f:
            f.write(script if script.endswith("\n") else script + "\n")
        return path

    def run_script(
        self,
        script_content: str,
        log_callback=None,
        timeout: int = DEFAULT_TIMEOUT,
        idle_timeout: int = DEFAULT_IDLE_TIMEOUT,
        quit_after: bool = True,
        keep_script: bool = False,
        script_dir: str = None,
    ) -> dict:
        """以 `+runscript <file>` 方式执行一段 steamcmd 脚本。

        用途：命令行参数拼接（`+cmd arg ...`）在命令较长、含特殊字符或需要
        多步流程（登录 / 切目录 / 多次下载 / 条件执行）时既脆弱又难调试；
        steamcmd 原生支持脚本文件，本方法即把脚本文本先落盘为临时文件，
        再交由 steamcmd 执行，结束后默认清理该文件。

        参数
        ----
        script_content : str
            脚本内容（本方法唯一必需参数）。每行一条命令，**不带 `+` 前缀**，
            例如::

                login anonymous
                force_install_dir D:/mods
                workshop_download_item 550 2807672660
                quit

            行首若写了 `+` 会自动剥离；`//` 开头的行是 steamcmd 注释。
        log_callback : callable
            逐行接收输出日志。
        timeout / idle_timeout : int
            总墙钟上限与空闲上限（语义同 `_run`）。
        quit_after : bool
            脚本末尾无 `quit` 时自动追加，保证 steamcmd 执行完退出
            （否则进程会停在控制台直到被 idle_timeout 判定卡死）。
        keep_script : bool
            True 则保留生成的脚本文件（便于排查），默认执行后删除。
        script_dir : str
            脚本文件落盘目录，默认系统临时目录。

        返回
        ----
        dict：`{"returncode", "errors", "logs", "script_path"}`
        （前三项同 `_run`，`script_path` 为本次生成的脚本文件绝对路径）
        """
        exe = self._resolve()
        if not exe.exists():
            raise SteamCMDError(f"steamcmd 不存在: {exe}")

        script = self._normalize_script(script_content)
        if not script:
            raise SteamCMDError("脚本内容为空，未执行 steamcmd")
        if quit_after and not self._has_quit(script):
            script += "\nquit"

        path = self._write_script(script, script_dir)
        try:
            res = self._run(
                [str(exe), "+runscript", str(path)],
                log_callback, timeout, idle_timeout,
            )
        finally:
            if not keep_script:
                try:
                    path.unlink(missing_ok=True)
                except Exception:  # noqa: BLE001
                    pass
        res["script_path"] = str(path)
        return res
